import json
import re
import logging
import time
from functools import wraps
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Callable, Tuple
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
    is_destination: bool = False
    destination_walking_time: int = 0

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

def update_lines_with_candidates(lines: List[Line], start_candidates: Dict[str, Dict[str, Any]], end_candidates: Dict[str, Dict[str, Any]]):
    """
    For each line, if candidate config exists (matching line code and direction),
    mark the station with target_station_code as active and set its walking_time.
    Also marks destination stations and their walking times.
    """
    logger.info("Updating lines with candidate information")
    for line in lines:
        # Update start stations
        start_c = start_candidates.get(line.line_code)
        if start_c and start_c.get("direction") == line.direction:
            tcode = start_c.get("target_station_code", "")
            wtime = start_c.get("walking_time", 7)
            for st in line.stations:
                if st.code == tcode:
                    st.active = True
                    st.walking_time = wtime
                    logger.debug(f"Marked start station '{st.name}' (code={st.code}) as active with walking_time={wtime}")
                    break
        
        # Update end stations
        end_c = end_candidates.get(line.line_code)
        if end_c and end_c.get("direction") == line.direction:
            tcode = end_c.get("target_station_code", "")
            wtime = end_c.get("walking_time", 7)
            for st in line.stations:
                if st.code == tcode:
                    st.is_destination = True
                    st.destination_walking_time = wtime
                    logger.debug(f"Marked end station '{st.name}' (code={st.code}) as destination with walking_time={wtime}")
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
        self._cache = {}  # Cache for API responses
        self._cache_lock = Lock()  # Lock for thread-safe caching
        self._cache_timeout = 60  # Cache timeout in seconds

    def _get_cache_key(self, station_code: str, line_code: str) -> str:
        """Generate a unique cache key for a station-line combination."""
        return f"{station_code}:{line_code}"

    def _is_cache_valid(self, cache_entry: Dict[str, Any]) -> bool:
        """Check if a cache entry is still valid."""
        if not cache_entry:
            return False
        return (time.time() - cache_entry.get("timestamp", 0)) < self._cache_timeout

    @timing_decorator
    def _get_waiting_time_single(self, station: Station, line_code: str) -> Optional[int]:
        cache_key = self._get_cache_key(station.code, line_code)
        
        # Check cache first
        with self._cache_lock:
            cache_entry = self._cache.get(cache_key)
            if self._is_cache_valid(cache_entry):
                logger.debug(f"Cache hit for station {station.name} (code={station.code})")
                return cache_entry.get("value")

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
                    result = parse_wait_message(raw_msg)
                    
                    # Cache the result
                    with self._cache_lock:
                        self._cache[cache_key] = {
                            "value": result,
                            "timestamp": time.time()
                        }
                    return result
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
        # First, check cache for all stations
        results = [None] * len(stations)
        stations_to_fetch = []
        station_indices = []

        for idx, station in enumerate(stations):
            cache_key = self._get_cache_key(station.code, line_code)
            with self._cache_lock:
                cache_entry = self._cache.get(cache_key)
                if self._is_cache_valid(cache_entry):
                    results[idx] = cache_entry.get("value")
                else:
                    stations_to_fetch.append(station)
                    station_indices.append(idx)

        if not stations_to_fetch:
            return results

        # Fetch only uncached stations
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_station = {
                executor.submit(self._get_waiting_time_single, station, line_code): idx
                for idx, station in enumerate(stations_to_fetch)
            }
            for future in as_completed(future_to_station):
                idx = future_to_station[future]
                try:
                    result = future.result()
                    results[station_indices[idx]] = result
                except Exception as e:
                    with self._lock:
                        logger.error(f"Error processing station at index {station_indices[idx]}: {e}")

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
        self._unique_stations = self._compute_unique_stations()
        self._line_travel_times = {}  # Cache for line travel times
        logger.info(f"TripPlanner initialized with {len(lines)} lines")

    def _compute_unique_stations(self) -> Dict[str, Station]:
        """Compute unique stations across all lines to avoid duplicate API calls."""
        unique_stations = {}
        for line in self.lines:
            for station in line.stations:
                if station.code not in unique_stations:
                    unique_stations[station.code] = station
        return unique_stations

    @timing_decorator
    def _gather_raw_waits(self, line: Line, candidate_idx: int) -> List[Optional[int]]:
        """Get all stations from candidate_idx down to 0 and fetch their waiting times."""
        stations_to_check = [line.stations[i] for i in range(candidate_idx, -1, -1)]
        return self.api.get_waiting_times_batch(stations_to_check, line.line_code)

    def _compute_line_travel_time(self, line: Line, start_station: Station) -> float:
        """Compute average travel time between stations for a line."""
        if line.line_code in self._line_travel_times:
            return self._line_travel_times[line.line_code]

        # Get all stations from start station down to 0
        stations_to_check = [line.stations[i] for i in range(start_station.index, -1, -1)]
        raw_list = self.api.get_waiting_times_batch(stations_to_check, line.line_code)
        
        if not raw_list:
            return 2.0  # Default value if no data

        numeric = [ (x if x is not None else 0) for x in raw_list ]
        differences = []
        for i in range(len(numeric) - 1):
            current = numeric[i]
            next_val = numeric[i+1]
            if next_val > current:
                break
            diff = current - next_val
            differences.append(diff)
        
        avg = sum(differences) / len(differences) if differences else 2.0
        avg = max(avg, 2.0)  # Ensure minimum of 2 minutes
        self._line_travel_times[line.line_code] = avg
        return avg

    def _compute_arrival(self, line: Line, station_idx: int, candidate_idx: int, raw_wait: int) -> float:
        """Compute arrival time at start station."""
        dist = candidate_idx - station_idx
        avg_time = self._line_travel_times.get(line.line_code, 2.0)
        return raw_wait + dist * avg_time

    def _compute_total_travel_time(self, line: Line, start_station: Station, end_station: Station) -> float:
        """Compute total travel time between start and end stations."""
        if start_station.index > end_station.index:
            return 0  # Invalid case
        distance = end_station.index - start_station.index
        avg_time = self._line_travel_times.get(line.line_code, 2.0)
        return distance * avg_time

    def _find_n_trams_increment(self, station: Station, line: Line, n: int = 3) -> List[Dict[str, Any]]:
        cidx = station.index
        walking_time = station.walking_time
        
        # Compute line travel time once
        avg_travel_time = self._compute_line_travel_time(line, station)
        logger.debug(f"Computed average travel time for line {line.line_code} = {avg_travel_time:.2f} minutes")
        
        raw_list = self._gather_raw_waits(line, cidx)
        logger.debug(f"Raw waits for line {line.line_code} (station idx={cidx} -> 0): {raw_list}")

        if not raw_list:
            return []

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
            arrival = self._compute_arrival(line, st_idx, cidx, rw)
            feasible = (arrival >= walking_time)
            wait_at_stop = arrival - walking_time if feasible else None
            results.append({
                "arrival": arrival,
                "feasible": feasible,
                "walk_time": walking_time,
                "wait_at_stop": wait_at_stop,
                "raw_wait": rw,
                "station_idx": st_idx,
                "avg_travel_time": avg_travel_time
            })
        return results[:n]

    def _find_destination_station(self, line: Line) -> Optional[Station]:
        """Find the destination station for a given line."""
        return next((s for s in line.stations if s.is_destination), None)

    @timing_decorator
    def plan_trip(self) -> Dict[str, List[Dict[str, Any]]]:
        with self._lock:
            if self._cached_plan is not None:
                return self._cached_plan

        logger.info("Planning trip for all lines")
        out = {}
        for line in self.lines:
            start_station = next((s for s in line.stations if s.active), None)
            if start_station:
                tram_list = self._find_n_trams_increment(start_station, line, n=3)
                if tram_list:
                    end_station = self._find_destination_station(line)
                    if end_station:
                        key = f"{start_station.name} -> {end_station.name} ({line.name}, Direction {line.direction})"
                        # Calculate travel time from start station to destination once
                        total_travel = self._compute_total_travel_time(
                            line, 
                            start_station,  # Always use the start station
                            end_station
                        )
                        for tram in tram_list:
                            tram["total_travel_time"] = total_travel
                            tram["final_walking_time"] = end_station.destination_walking_time
                            tram["total_time"] = (
                                tram["arrival"] + 
                                total_travel + 
                                end_station.destination_walking_time
                            )
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
        feasible_options.sort(key=lambda x: x[1]["total_time"])
        best_station_line, best_info = feasible_options[0]
        return {"station_line": best_station_line, "tram": best_info}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    logger.info("Starting ATM trip planner with multi-threading and destination support")
    start_time = time.time()
    
    lines_file = "/Users/andre/Desktop/Coding/Python/tenv/Projects/atm/lines.json"
    lines = load_lines_from_file(lines_file)
    
    start_candidates = {
        "15": {"direction": "0", "target_station_code": "15371", "walking_time": 8},
        "3":  {"direction": "0", "target_station_code": "11139", "walking_time": 4},
        "59": {"direction": "0", "target_station_code": "11154", "walking_time": 8}
    }
    
    end_candidates = {
        "15": {"direction": "0", "target_station_code": "15379", "walking_time": 4},
        "3":  {"direction": "0", "target_station_code": "11443", "walking_time": 7},
        "59": {"direction": "0", "target_station_code": "11459", "walking_time": 3}
    }
    
    update_lines_with_candidates(lines, start_candidates, end_candidates)
    
    metro_api = MetroAPI(max_workers=10)  # Adjust max_workers as needed
    planner = TripPlanner(lines, metro_api)
    
    print("\n" + "="*80)
    print("ATM TRIP PLANNER RESULTS")
    print("="*80)
    
    trip_plan = planner.plan_trip()
    print("\nAvailable Routes:")
    print("-"*80)
    
    for station_line, tram_infos in trip_plan.items():
        print(f"\nRoute: {station_line}")
        print("  " + "-"*40)
        for i, info in enumerate(tram_infos, start=1):
            status = "✓" if info["feasible"] else "✗"
            print(f"  Tram #{i} [{status}]")
            print(f"    Time Breakdown:")
            print(f"      • Initial walking: {info['walk_time']} min")
            print(f"      • Wait at stop: {info['wait_at_stop'] if info['wait_at_stop'] is not None else 'N/A'} min")
            print(f"      • Travel time: {info['total_travel_time']:.1f} min")
            print(f"      • Final walking: {info['final_walking_time']} min")
            print(f"      • Total journey: {info['total_time']:.1f} min")
            print(f"    Details:")
            print(f"      • Arrival at start: {info['arrival']:.1f} min")
            print(f"      • Raw wait time: {info['raw_wait']} min")
            print(f"      • Starting from station index: {info['station_idx']}")
            print(f"      • Average travel time between stations: {info['avg_travel_time']:.1f} min")
    
    best = planner.best_tram()
    print("\n" + "="*80)
    print("BEST OPTION")
    print("="*80)
    
    if best is None:
        print("\n❌ No feasible tram found.")
    else:
        sl = best["station_line"]
        tinfo = best["tram"]
        print(f"\nRoute: {sl}")
        print("\nJourney Details:")
        print("  " + "-"*40)
        print(f"  • Initial walking: {tinfo['walk_time']} min")
        print(f"  • Wait at stop: {tinfo['wait_at_stop']} min")
        print(f"  • Travel time: {tinfo['total_travel_time']:.1f} min")
        print(f"  • Final walking: {tinfo['final_walking_time']} min")
        print(f"  • Total journey time: {tinfo['total_time']:.1f} min")
        print(f"\nTiming Breakdown:")
        print(f"  • Arrives at start in: {tinfo['arrival']:.1f} min")
        print(f"  • Travels to destination in: {tinfo['total_travel_time']:.1f} min")
        print(f"  • Final walk takes: {tinfo['final_walking_time']} min")
    
    end_time = time.time()
    total_duration = end_time - start_time
    print("\n" + "="*80)
    print(f"Execution completed in {total_duration:.2f} seconds")
    print("="*80 + "\n")

if __name__ == "__main__":
    main() 