import json
import re
import logging
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from curl_cffi import requests as curlq
import requests

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
# Utility Functions and Data Classes
# --------------------------------------------------------------------------

def parse_wait_message(wait_message: str) -> Optional[int]:
    """Extract an integer wait from 'X min' or 'in arrivo' (returns 1); otherwise None."""
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
    For each line, if the candidate configuration matches (by line code and direction),
    mark the station with the target station code as active and assign its walking_time.
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
# Metro API Client
# --------------------------------------------------------------------------

class MetroAPI:
    """
    A simple API client that fetches the "next tram" waiting time from the ATM endpoint.
    """
    def get_waiting_time(self, station: Station, line_code: str) -> Optional[int]:
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
            logger.error(f"Request error at station {station.name} (code={station.code}): {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error at station {station.name} (code={station.code}): {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error at station {station.name} (code={station.code}): {e}")
            return None

# --------------------------------------------------------------------------
# Trip Planner with Caching and Increment-based Detection
# --------------------------------------------------------------------------

class TripPlanner:
    """
    This TripPlanner:
      - Gathers raw waits from the candidate station (index) down to 0.
      - Converts None to 0.
      - Uses the simple rule: if raw_wait[i] > raw_wait[i-1] in the reversed list, that's a new tram.
      - Converts each raw wait to an arrival time at the candidate station.
      - Checks feasibility based on walking_time.
      - Caches the plan so that API calls are not repeated.
    """
    def __init__(self, lines: List[Line], api: MetroAPI):
        self.lines = lines
        self.api = api
        self._cached_plan = None  # Cache for plan_trip results
        logger.info(f"TripPlanner initialized with {len(lines)} lines")

    def _gather_raw_waits(self, line: Line, candidate_idx: int) -> List[Optional[int]]:
        raw_waits = []
        for i in range(candidate_idx, -1, -1):
            st = line.stations[i]
            w = self.api.get_waiting_time(st, line.line_code)
            raw_waits.append(w)
        return raw_waits

    def _compute_arrival(self, line: Line, station_idx: int, candidate_idx: int, raw_wait: int) -> int:
        dist = candidate_idx - station_idx
        return raw_wait + dist * line.travel_time_between_stations

    def _find_two_trams_increment(self, station: Station, line: Line) -> List[Dict[str, Any]]:
        """
        Uses the simple rule: the first raw wait in the reversed list is tram #1.
        Then, for each subsequent element, if it is greater than the previous one,
        that's considered a new tram.
        """
        cidx = station.index
        walking_time = station.walking_time
        raw_list = self._gather_raw_waits(line, cidx)
        logger.debug(f"Raw waits for line {line.line_code} (station idx={cidx} -> 0): {raw_list}")

        if not raw_list:
            return []

        # Convert None to 0 for comparisons
        numeric = [(x if x is not None else 0) for x in raw_list]
        found = []
        # Tram #1 is the first element (from candidate station)
        found.append({
            "raw_wait": numeric[0],
            "station_idx": cidx
        })
        # Look for the first occurrence where the next element is greater than the previous element
        for i in range(1, len(numeric)):
            if numeric[i] > numeric[i - 1]:
                found.append({
                    "raw_wait": numeric[i],
                    "station_idx": cidx - i
                })
                break  # only two trams

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
                "station_idx": st_idx
            })
        return results[:2]

    def plan_trip(self) -> Dict[str, List[Dict[str, Any]]]:
        if self._cached_plan is not None:
            return self._cached_plan

        logger.info("Planning trip for all lines")
        out = {}
        for line in self.lines:
            cand = next((s for s in line.stations if s.active), None)
            if cand:
                tram_list = self._find_two_trams_increment(cand, line)
                if tram_list:
                    key = f"{cand.name} ({line.name}, Direction {line.direction})"
                    out[key] = tram_list
        self._cached_plan = out
        return out

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
    logger.info("Starting ATM trip planner")
    # Adjust the file path as needed.
    lines_file = "/Users/andre/Desktop/Coding/Python/tenv/Projects/atm/lines.json"
    lines = load_lines_from_file(lines_file)

    # Candidate configuration: line code mapped to target station code and walking time.
    candidates = {
        "15": {"direction": "0", "target_station_code": "15371", "walking_time": 8},
        "3":  {"direction": "0", "target_station_code": "11139", "walking_time": 4}
    }
    update_lines_with_candidates(lines, candidates)

    metro_api = MetroAPI()
    planner = TripPlanner(lines, metro_api)

    trip_plan = planner.plan_trip()
    print("\nFeasible tram times for candidate stations (up to 2 trams each):")
    for station_line, tram_infos in trip_plan.items():
        print(f"\n{station_line}:")
        for i, info in enumerate(tram_infos, start=1):
            print(f"  Tram #{i}: arrival={info['arrival']} min, "
                  f"feasible={info['feasible']}, walk_time={info['walk_time']}, "
                  f"wait_at_stop={info['wait_at_stop']}, raw_wait={info['raw_wait']}")

    best = planner.best_tram()
    if best is None:
        print("\nNo feasible tram found.")
    else:
        sl = best["station_line"]
        tinfo = best["tram"]
        print(f"\nBest tram option:\n{sl}")
        print(f" - Arrives in {tinfo['arrival']} minutes.")
        print(f" - Walking time is {tinfo['walk_time']} minutes => you'll wait {tinfo['wait_at_stop']} minutes at the stop.")

if __name__ == "__main__":
    main()