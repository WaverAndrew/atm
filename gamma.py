import json
import re
import logging
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple
from curl_cffi import requests as curlq
import requests  # Only for exception handling

# --------------------------------------------------------------------------
# LOGGING CONFIG
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('atm_debug.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# UTILITY FUNCTIONS AND DATA CLASSES
# --------------------------------------------------------------------------

def parse_wait_message(wait_message: str) -> Optional[int]:
    """
    Parses the wait message from the API and returns a numeric waiting time in minutes.
    - If the message contains "min", extracts the integer.
    - If the message is "in arrivo", return 1 minute.
    - If it's "updating" or None, returns None.
    """
    if not wait_message:
        return None
    msg = wait_message.strip().lower()
    if "min" in msg:
        match = re.search(r"(\d+)", msg)
        if match:
            return int(match.group(1))
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
        index = st.get("index", 0)
        name = st.get("name", "Unknown")
        code = st.get("code", "")
        station = Station(name=name, code=code, walking_time=0, index=index, active=False)
        stations_list.append(station)
    stations_list.sort(key=lambda s: s.index)

    return Line(
        name=line_description,
        line_code=line_code,
        direction=direction,
        stations=stations_list
    )

def load_lines_from_file(filename: str) -> List[Line]:
    logger.info(f"Loading lines from file: {filename}")
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        lines_data = data.get("lines", [])
        lines = [load_line_data(line_obj) for line_obj in lines_data]
        logger.info(f"Successfully loaded {len(lines)} lines")
        return lines
    except Exception as e:
        logger.error(f"Error loading lines from file: {e}", exc_info=True)
        raise

def update_lines_with_candidates(lines: List[Line], candidates: Dict[str, Dict[str, any]]):
    """
    Mark the station specified in 'candidates' as active and assign the walking_time.
    Each candidate entry has shape:
      {
          "direction": "0",
          "target_station_code": "XYZ",
          "walking_time": 8
      }
    """
    logger.info("Updating lines with candidate information")
    for line in lines:
        cand = candidates.get(line.line_code)
        if cand and cand.get("direction") == line.direction:
            target_station_code = cand.get("target_station_code")
            walking_time = cand.get("walking_time", 7)
            for station in line.stations:
                if station.code == target_station_code:
                    station.active = True
                    station.walking_time = walking_time
                    logger.debug(f"Marked station '{station.name}' code={station.code} as active with walking_time={walking_time}")
                    break

# --------------------------------------------------------------------------
# API CLIENT
# --------------------------------------------------------------------------

class MetroAPI:
    """
    Fetches the single next arrival time from the ATM 'linesummary' endpoint.
    """
    def get_waiting_time(self, station: Station, target_line_code: str) -> Optional[int]:
        base_url = "https://giromilano.atm.it/proxy.tpportal/api/tpPortal"
        url = f"{base_url}/tpl/stops/{station.code}/linesummary"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9,it;q=0.8',
            'Origin': 'https://giromilano.atm.it',
            'Referer': 'https://giromilano.atm.it/'
        }
        try:
            logger.debug(f"API request for station {station.name} (code={station.code}), line={target_line_code}")
            resp = curlq.get(url, headers=headers, impersonate="chrome")
            resp.raise_for_status()
            data = resp.json()

            for line_info in data.get("Lines", []):
                # Compare line codes
                if line_info.get("Line", {}).get("LineCode") == target_line_code:
                    raw_msg = line_info.get("WaitMessage")
                    if raw_msg is None:
                        logger.debug(f"No WaitMessage for station {station.code}")
                        return None
                    return parse_wait_message(raw_msg)
            return None

        except requests.RequestException as e:
            logger.error(f"Request error for station {station.name} (code={station.code}): {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"JSON parsing error for station {station.name} (code={station.code}): {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error for station {station.name} (code={station.code}): {e}")
            return None

# --------------------------------------------------------------------------
# TRIP PLANNER WITH BACKTRACKING
# --------------------------------------------------------------------------

class TripPlanner:
    """
    A TripPlanner that, for each line's candidate station, finds up to two distinct trams
    by backtracking upstream. 
    """

    def __init__(self, lines: List[Line], api: MetroAPI):
        self.lines = lines
        self.api = api
        logger.info(f"TripPlanner initialized with {len(lines)} lines")

    def _get_two_trams_by_backtracking(self, station: Station, line: Line) -> List[int]:
        """
        Returns up to two distinct arrival times (in minutes) at the candidate station
        by scanning backwards and identifying whenever an upstream station reveals
        a *different* (later) tram.

        Process:
          1) Check candidate station's own wait time for the first tram (if valid).
          2) Move upstream station by station (index=station.index-1 down to 0):
             - For each station i, fetch the 'raw wait' for that station.
             - Compute arrival at candidate = raw_wait + (distance * travel_time).
             - If we haven't found the first tram, take it if it's >= walking_time.
             - If we have found the first, only take it if it indicates a strictly
               *later* tram (meaning raw_wait is significantly bigger than the first's wait).
          3) Stop once you have two distinct trams or run out of stations.

        Returns a list: [firstArrival] or [firstArrival, secondArrival], sorted ascending.
        """
        logger.debug(f"=== Backtracking for line {line.line_code} candidate station '{station.name}' ===")

        candidate_idx = station.index
        travel_time = line.travel_time_between_stations
        walking_time = station.walking_time

        result_times = []

        # 1) Attempt to get the "next tram" wait at the candidate station itself.
        candidate_wait = self.api.get_waiting_time(station, line.line_code)
        first_tram_wait = None  # raw wait that identifies the first tram (could come from candidate or upstream)

        if candidate_wait is not None and candidate_wait >= walking_time:
            arrival = candidate_wait  # distance=0 from itself
            logger.debug(
                f"Candidate station direct wait = {candidate_wait} (>= walking time={walking_time}), "
                "taking it as first tram"
            )
            result_times.append(arrival)
            first_tram_wait = candidate_wait
        else:
            logger.debug(f"Candidate station direct wait is invalid or < walking time, skipping direct station check")

        # 2) Go upstream from station (candidate_idx-1) down to 0 to find:
        #    - If we didn't get a first tram yet, get the first feasible one.
        #    - If we did get a first tram, see if we can find a second that's definitely a new tram.
        for i in range(candidate_idx - 1, -1, -1):
            st_up = line.stations[i]
            raw_wait = self.api.get_waiting_time(st_up, line.line_code)
            logger.debug(f"[Station index={i}] '{st_up.name}', raw_wait={raw_wait}")

            if raw_wait is None:
                continue

            distance = (candidate_idx - i)
            arrival_at_candidate = raw_wait + distance * travel_time
            logger.debug(
                f"    -> arrival_at_candidate = {raw_wait} + ({distance} * {travel_time}) "
                f"= {arrival_at_candidate}"
            )

            if arrival_at_candidate < walking_time:
                logger.debug(f"    -> REJECTED: arrival < walking_time ({walking_time})")
                continue

            if first_tram_wait is None:
                # We haven't found a first tram at all, so let's accept this one as #1
                first_tram_wait = raw_wait
                logger.debug(
                    f"    -> First tram set from station index {i}, arrival={arrival_at_candidate}, "
                    f"raw_wait={raw_wait}"
                )
                result_times.append(arrival_at_candidate)
            else:
                # We already have a first tram. We want to see if raw_wait indicates a *newer* tram.
                # If raw_wait is significantly greater than the first tram's raw wait,
                # that means it's a behind-later tram. We'll pick it as #2 and stop.
                # We'll use a small threshold, e.g. (raw_wait > first_tram_wait + 1).
                if raw_wait > first_tram_wait + 1:
                    logger.debug(
                        f"    -> Second tram found! raw_wait={raw_wait} > first_tram_wait={first_tram_wait}+1, "
                        f"arrival={arrival_at_candidate}"
                    )
                    result_times.append(arrival_at_candidate)
                    break  # stop searching; we only want 2
                else:
                    logger.debug(
                        f"    -> Still the same tram (raw_wait={raw_wait} not > first_tram_wait={first_tram_wait}+1). Skipping."
                    )

        # Sort final times ascending
        result_times.sort()
        # Remove duplicates
        unique = []
        for t in result_times:
            if not unique or t != unique[-1]:
                unique.append(t)

        # Return up to 2
        final_two = unique[:2]
        logger.debug(f"Final arrival times at candidate station: {final_two}")
        return final_two

    def plan_trip(self) -> Dict[str, List[int]]:
        """
        For each line, if there's an active candidate station, backtrack to find up to two distinct trams.
        Returns a dict with "StationName (LineName, Direction X)" -> [time1, time2].
        """
        logger.info("Planning trip for all lines")
        feasible_trams = {}

        for line in self.lines:
            candidate_station = next((s for s in line.stations if s.active), None)
            if candidate_station:
                times = self._get_two_trams_by_backtracking(candidate_station, line)
                if times:
                    key = f"{candidate_station.name} ({line.name}, Direction {line.direction})"
                    feasible_trams[key] = times

        return feasible_trams

    def best_tram(self) -> Optional[Dict[str, any]]:
        """
        Among all lines' candidate stations, picks the one with the smallest first tram arrival.
        Returns {"station_line": key, "waiting_times": [...]}, or None if none found.
        """
        feasible = self.plan_trip()
        best = None
        best_wait = float('inf')
        for station_line, times in feasible.items():
            if times and times[0] < best_wait:
                best_wait = times[0]
                best = {"station_line": station_line, "waiting_times": times}
        return best

# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def main():
    logger.info("Starting ATM trip planner")

    # Change this file path to wherever your lines.json is located
    lines_file = "/Users/andre/Desktop/Coding/Python/tenv/Projects/atm/lines.json"

    try:
        # 1) Load lines from JSON
        lines = load_lines_from_file(lines_file)

        # 2) Mark candidate stations
        candidates = {
            # Example candidates
            "15": {"direction": "0", "target_station_code": "15371", "walking_time": 8},
            "3":  {"direction": "0", "target_station_code": "11139", "walking_time": 4}
        }
        update_lines_with_candidates(lines, candidates)

        # 3) Build the ATM API client
        metro_api = MetroAPI()

        # 4) Create the TripPlanner with backtracking logic
        planner = TripPlanner(lines, metro_api)

        # 5) Plan the trip
        trip_plan = planner.plan_trip()

        print("\nFeasible tram times for candidate stations:")
        for station_line, times in trip_plan.items():
            print(f"{station_line}: {times} minutes")

        # 6) Best option
        best = planner.best_tram()
        if best:
            print("\nBest tram option:")
            print(f"{best['station_line']} with waiting times {best['waiting_times']} minutes")
        else:
            print("\nNo feasible tram option found.")

    except Exception as e:
        logger.error(f"Fatal error in main: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()