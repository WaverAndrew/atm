import json
import re
import logging
import time
from functools import wraps
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Callable
from curl_cffi import requests as curlq
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# --------------------------------------------------------------------------
# Logging Configuration
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("atm_debug.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Timing Decorator
# --------------------------------------------------------------------------
def timing_decorator(func: Callable) -> Callable:
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        duration = end_time - start_time
        print(f"[TIMING] {func.__name__} took {duration:.2f} seconds")
        return result
    return wrapper

# --------------------------------------------------------------------------
# Utility Functions and Data Classes
# --------------------------------------------------------------------------
def parse_wait_message(wait_message: str) -> Optional[int]:
    """Extract integer wait from 'X min' or 'in arrivo' (returns 1); otherwise None."""
    if not wait_message:
        return None
    msg = wait_message.strip().lower()
    if "min" in msg:
        m = re.search(r"(\d+)", msg)
        if m:
            return int(m.group(1))
    elif msg == "in arrivo":
        return 1
    elif msg == "updating":
        return None
    return None

@dataclass
class Station:
    name: str
    code: str
    walking_time: int
    index: int
    active: bool

@dataclass
class Line:
    name: str
    line_code: str
    direction: str
    stations: List[Station]
    travel_time_between_stations: int = 2

def load_line_data(data: dict) -> Line:
    line_info = data.get("line", {})
    line_code = line_info.get("code", "")
    line_description = line_info.get("description", "")
    direction = data.get("direction", "")
    stations_list = []
    for st in data.get("stations", []):
        idx = st.get("index", 0)
        nm = st.get("name", "Unknown")
        cd = st.get("code", "")
        stations_list.append(Station(name=nm, code=cd, walking_time=0, index=idx, active=False))
    stations_list.sort(key=lambda s: s.index)
    return Line(name=line_description, line_code=line_code, direction=direction, stations=stations_list)

def load_lines_from_file(filename: str) -> List[Line]:
    logger.info(f"Loading lines from file: {filename}")
    with open(filename, "r", encoding="utf-8") as f:
        data = json.load(f)
    lines_data = data.get("lines", [])
    lines = [load_line_data(ld) for ld in lines_data]
    logger.info(f"Successfully loaded {len(lines)} lines")
    return lines

def update_lines_with_candidates(lines: List[Line], candidates: Dict[str, Dict[str, Any]]):
    """
    For each line, if candidate config exists (matching line code and direction),
    mark the station with target_station_code as active and set its walking_time.
    """
    logger.info("Updating lines with candidate information")
    for line in lines:
        c = candidates.get(line.line_code)
        if c and c.get("direction") == line.direction:
            tcode = c.get("target_station_code", "")
            wtime = c.get("walking_time", 7)
            for st in line.stations:
                if st.code == tcode:
                    st.active = True
                    st.walking_time = wtime
                    logger.debug(f"Marked station '{st.name}' (code={st.code}) as active with walking_time={wtime}")
                    break

# --------------------------------------------------------------------------
# Metro API Client with Multi-threading
# --------------------------------------------------------------------------
class MetroAPI:
    """
    Fetches the "next tram" wait time from the ATM endpoint using concurrent requests.
    """
    def __init__(self, max_workers: int = 10):
        self.max_workers = max_workers
        self._lock = Lock()  # For thread-safe logging
        self.total_api_calls = 0
        self.total_api_time = 0.0

    @timing_decorator
    def _get_waiting_time_single(self, station: Station, line_code: str) -> Optional[int]:
        base_url = "https://giromilano.atm.it/proxy.tpportal/api/tpPortal"
        url = f"{base_url}/tpl/stops/{station.code}/linesummary"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Origin": "https://giromilano.atm.it",
            "Referer": "https://giromilano.atm.it/"
        }
        try:
            resp = curlq.get(url, headers=headers, impersonate="chrome")
            resp.raise_for_status()
            data = resp.json()
            for line_obj in data.get("Lines", []):
                if line_obj.get("Line", {}).get("LineCode") == line_code:
                    raw_msg = line_obj.get("WaitMessage")
                    return parse_wait_message(raw_msg)
            return None
        except requests.RequestException as e:
            with self._lock:
                logger.error(f"Request error at station {station.name} (code={station.code}): {e}")
            return None
        except json.JSONDecodeError as e:
            with self._lock:
                logger.error(f"JSON parse error at station {station.name} (code={station.code}): {e}")
            return None
        except Exception as e:
            with self._lock:
                logger.error(f"Unexpected error at station {station.name} (code={station.code}): {e}")
            return None

    @timing_decorator
    def get_waiting_times_batch(self, stations: List[Station], line_code: str) -> List[Optional[int]]:
        """
        Fetches waiting times for multiple stations concurrently.
        """
        results = [None] * len(stations)
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_station = {
                executor.submit(self._get_waiting_time_single, station, line_code): idx
                for idx, station in enumerate(stations)
            }
            for future in as_completed(future_to_station):
                idx = future_to_station[future]
                try:
                    results[idx] = future.result()
                except Exception as e:
                    with self._lock:
                        logger.error(f"Error processing station at index {idx}: {e}")
        return results

# --------------------------------------------------------------------------
# Trip Planner with Caching, Three Trams, and Average Travel Time
# --------------------------------------------------------------------------
class TripPlanner:
    """
    TripPlanner that uses concurrent API calls to gather data faster.
    """
    def __init__(self, lines: List[Line], api: MetroAPI):
        self.lines = lines
        self.api = api
        self._cached_plan = None
        self._lock = Lock()  # For thread-safe caching
        logger.info(f"TripPlanner initialized with {len(lines)} lines")

    @timing_decorator
    def _gather_raw_waits(self, line: Line, candidate_idx: int) -> List[Optional[int]]:
        # Get all stations from candidate_idx down to 0
        stations_to_check = [line.stations[i] for i in range(candidate_idx, -1, -1)]
        # Use batch API call to get all waiting times concurrently
        return self.api.get_waiting_times_batch(stations_to_check, line.line_code)

    def _compute_average_travel_time(self, raw_list: List[Optional[int]]) -> float:
        """
        Computes the average travel time from the candidate station's contiguous segment
        of nonincreasing raw waits.
        """
        numeric = [ (x if x is not None else 0) for x in raw_list ]
        differences = []
        for i in range(len(numeric) - 1):
            current = numeric[i]
            next_val = numeric[i+1]
            if next_val > current:
                break
            diff = current - next_val
            differences.append(diff)
        if differences:
            avg = sum(differences) / len(differences)
            return avg if avg > 0 else 2
        else:
            return 2

    def _compute_arrival(self, line: Line, station_idx: int, candidate_idx: int, raw_wait: int, avg_time: float) -> float:
        dist = candidate_idx - station_idx
        return raw_wait + dist * avg_time

    @timing_decorator
    def _find_n_trams_increment(self, station: Station, line: Line, n: int = 3) -> List[Dict[str, Any]]:
        cidx = station.index
        walking_time = station.walking_time
        raw_list = self._gather_raw_waits(line, cidx)
        logger.debug(f"Raw waits for line {line.line_code} (station idx={cidx} -> 0): {raw_list}")

        if not raw_list:
            return []

        avg_travel_time = self._compute_average_travel_time(raw_list)
        logger.debug(f"Computed average travel time for line {line.line_code} = {avg_travel_time:.2f} minutes")

        numeric = [ (x if x is not None else 0) for x in raw_list ]
        found = []
        found.append({
            "raw_wait": numeric[0],
            "station_idx": cidx
        })
        for i in range(1, len(numeric)):
            if len(found) >= n:
                break
            if numeric[i] > numeric[i - 1]:
                found.append({
                    "raw_wait": numeric[i],
                    "station_idx": cidx - i
                })
        results = []
        for tram in found:
            rw = tram["raw_wait"]
            st_idx = tram["station_idx"]
            arrival = self._compute_arrival(line, st_idx, cidx, rw, avg_travel_time)
            feasible = (arrival >= walking_time)
            wait_at_stop = arrival - walking_time if feasible else None
            results.append({
                "arrival": arrival,
                "feasible": feasible,
                "walk_time": walking_time,
                "wait_at_stop": wait_at_stop,
                "raw_wait": rw,
                "station_idx": st_idx
            })
        return results[:n]

    @timing_decorator
    def plan_trip(self) -> Dict[str, List[Dict[str, Any]]]:
        with self._lock:
            if self._cached_plan is not None:
                return self._cached_plan

        logger.info("Planning trip for all lines")
        out = {}
        for line in self.lines:
            cand = next((s for s in line.stations if s.active), None)
            if cand:
                tram_list = self._find_n_trams_increment(cand, line, n=3)
                if tram_list:
                    key = f"{cand.name} ({line.name}, Direction {line.direction})"
                    out[key] = tram_list

        with self._lock:
            self._cached_plan = out
        return out

    @timing_decorator
    def best_tram(self) -> Optional[Dict[str, Any]]:
        feasible_options = []
        trip_plan = self.plan_trip()
        for station_line, tram_infos in trip_plan.items():
            for info in tram_infos:
                if info["feasible"]:
                    feasible_options.append((station_line, info))
        if not feasible_options:
            return None
        feasible_options.sort(key=lambda x: x[1]["arrival"])
        best_station_line, best_info = feasible_options[0]
        return {"station_line": best_station_line, "tram": best_info}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    logger.info("Starting ATM trip planner with multi-threading")
    start_time = time.time()
    
    lines_file = "/Users/andre/Desktop/Coding/Python/tenv/Projects/atm/lines.json"
    lines = load_lines_from_file(lines_file)
    
    candidates = {
        "15": {"direction": "0", "target_station_code": "15371", "walking_time": 8},
        "3":  {"direction": "0", "target_station_code": "11139", "walking_time": 4},
        "59": {"direction": "0", "target_station_code": "11154", "walking_time": 8}
    }
    """
    candidates = {
        "15": {"direction": "1", "target_station_code": "15380", "walking_time": 6},
        "3":  {"direction": "1", "target_station_code": "11445", "walking_time": 10},
        "59": {"direction": "1", "target_station_code": "11466", "walking_time": 5}
    }"""
    
    update_lines_with_candidates(lines, candidates)
    
    metro_api = MetroAPI(max_workers=10)  # Adjust max_workers as needed
    planner = TripPlanner(lines, metro_api)
    
    print("\n=== Performance Metrics (Threaded Version) ===")
    trip_plan = planner.plan_trip()
    print("\nFeasible tram times for candidate stations (up to 3 trams each):")
    for station_line, tram_infos in trip_plan.items():
        print(f"\n{station_line}:")
        for i, info in enumerate(tram_infos, start=1):
            print(f"  Tram #{i}: arrival={info['arrival']:.1f} min, "
                  f"feasible={info['feasible']}, walk_time={info['walk_time']} min, "
                  f"wait_at_stop={info['wait_at_stop'] if info['wait_at_stop'] is not None else 'N/A'} min, "
                  f"raw_wait={info['raw_wait']}, from station index {info['station_idx']}")
    
    best = planner.best_tram()
    if best is None:
        print("\nNo feasible tram found.")
    else:
        sl = best["station_line"]
        tinfo = best["tram"]
        print(f"\nBest tram option:\n{sl}")
        print(f" - Arrives in {tinfo['arrival']:.1f} minutes.")
        print(f" - Walking time is {tinfo['walk_time']} min => you'll wait {tinfo['wait_at_stop']} min at the stop.")
    
    end_time = time.time()
    total_duration = end_time - start_time
    print(f"\nTotal execution time: {total_duration:.2f} seconds")

if __name__ == "__main__":
    main() 