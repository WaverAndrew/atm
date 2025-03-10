import json
import re
import logging
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple
from curl_cffi import requests as curlq
import requests  # For exception handling

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('atm_debug.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def parse_wait_message(wait_message: str) -> Optional[int]:
    """
    Parses the wait message from the API and returns a numeric waiting time in minutes.
    
    - If the message ends with "min" (e.g., "15 min"), extracts and returns the integer.
    - If the message is "in arrivo", returns 1 (i.e. under 2 minutes).
    - If the message is "updating" or any other non-numeric string, returns None.
    """
    logger.debug(f"Parsing wait message: '{wait_message}'")
    if not wait_message:
        logger.debug("Empty wait message received")
        return None
    wait_message = wait_message.strip().lower()
    if "min" in wait_message:
        match = re.search(r"(\d+)", wait_message)
        if match:
            result = int(match.group(1))
            logger.debug(f"Extracted waiting time: {result} minutes")
            return result
    elif wait_message == "in arrivo":
        logger.debug("Tram is arriving (1 minute)")
        return 1
    elif wait_message == "updating":
        logger.debug("Message indicates updating status")
        return None
    logger.debug(f"Could not parse wait message: '{wait_message}'")
    return None

# Data model for a station.
@dataclass
class Station:
    name: str
    code: str          # Station code used for API calls.
    walking_time: int  # Minutes to walk from home (0 if not the candidate).
    index: int         # Position of the station along the line.
    active: bool       # True if this station is the candidate departure stop.

# Data model for a transit line.
@dataclass
class Line:
    name: str
    line_code: str     # The target line code (e.g. "15" or "3").
    direction: str     # The direction for this line.
    stations: List[Station]
    travel_time_between_stations: int = 2  # Minutes between adjacent stations.
    # Note: We no longer use a fixed tram frequency.

def load_line_data(data: dict) -> Line:
    """
    Loads a Line object from a JSON-like dictionary.
    Expected keys:
      - "line": contains the line code and description.
      - "direction": the direction for the line.
      - "stations": a list of station objects with "index", "name", and "code".
    Initially, no station is marked as active (walking_time set to 0).
    """
    line_info = data.get("line", {})
    line_code = line_info.get("code", "")
    line_description = line_info.get("description", "")
    direction = data.get("direction", "")
    stations_list = []
    for st in data.get("stations", []):
        index = st.get("index", 0)
        name = st.get("name", "Unknown")
        code = st.get("code", "")
        # Initially, walking_time is 0 and active is False.
        station = Station(name=name, code=code, walking_time=0, index=index, active=False)
        stations_list.append(station)
    stations_list.sort(key=lambda s: s.index)
    return Line(name=line_description, line_code=line_code, direction=direction, stations=stations_list)

def load_lines_from_file(filename: str) -> List[Line]:
    """
    Loads an array of line objects from a JSON file and returns a list of Line instances.
    The JSON file is expected to have a top-level "lines" key.
    """
    logger.info(f"Loading lines from file: {filename}")
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        lines_data = data.get("lines", [])
        logger.debug(f"Found {len(lines_data)} lines in the file")
        lines = [load_line_data(line_obj) for line_obj in lines_data]
        logger.info(f"Successfully loaded {len(lines)} lines")
        return lines
    except Exception as e:
        logger.error(f"Error loading lines from file: {e}")
        raise

def update_lines_with_candidates(lines: List[Line], candidates: Dict[str, Dict[str, any]]):
    """
    For each line in the list, if its line_code is present in the candidates dictionary
    and its direction matches candidate info, mark the station with the matching candidate
    station code as active and update its walking_time.
    
    The candidates dict should be of the form:
      {
         "15": {"direction": "0", "target_station_code": "13711", "walking_time": 8},
         "3":  {"direction": "0", "target_station_code": "37472", "walking_time": 10}
      }
    """
    logger.info("Updating lines with candidate information")
    logger.debug(f"Candidates data: {candidates}")
    
    for line in lines:
        logger.debug(f"Processing line {line.line_code} (direction: {line.direction})")
        if line.line_code in candidates:
            candidate = candidates[line.line_code]
            logger.debug(f"Found candidate for line {line.line_code}: {candidate}")
            if candidate.get("direction") == line.direction:
                target_station_code = candidate.get("target_station_code")
                walking_time = candidate.get("walking_time", 7)
                logger.debug(f"Looking for station with code {target_station_code}")
                for station in line.stations:
                    if station.code == target_station_code:
                        station.active = True
                        station.walking_time = walking_time
                        logger.info(f"Updated station {station.name} (code: {station.code}) as active with walking time {walking_time}")
                        break

# Metro API client using the new endpoint.
class MetroAPI:
    def get_waiting_time(self, station: Station, target_line_code: str) -> Optional[int]:
        """
        Fetches the waiting time for a given station and target line code from the ATM API.
        Uses the endpoint:
           https://giromilano.atm.it/proxy.tpportal/api/tpPortal/tpl/stops/{station_code}/linesummary
        Returns a numeric waiting time in minutes (or None if unavailable).
        """
        logger.debug(f"Fetching waiting time for station {station.code} on line {target_line_code}")
        base_url = "https://giromilano.atm.it/proxy.tpportal/api/tpPortal"
        url = f"{base_url}/tpl/stops/{station.code}/linesummary"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9,it;q=0.8',
            'Origin': 'https://giromilano.atm.it',
            'Referer': 'https://giromilano.atm.it/'
        }
        try:
            logger.debug(f"Making API request to: {url}")
            response = curlq.get(url, headers=headers, impersonate="chrome")
            response.raise_for_status()
            data = response.json()
            logger.debug(f"API response for station {station.code}: {data}")
            
            for line in data.get("Lines", []):
                if line.get("Line", {}).get("LineCode", "") == target_line_code:
                    raw_wait_message = line.get("WaitMessage", "").strip()
                    logger.debug(f"Found wait message for line {target_line_code}: '{raw_wait_message}'")
                    return parse_wait_message(raw_wait_message)
            logger.warning(f"No data found for line {target_line_code} at station {station.code}")
            return None
        except requests.RequestException as e:
            logger.error(f"Request error for station {station.code}: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"JSON parsing error for station {station.code}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error for station {station.code}: {e}")
            return None

# Trip planner with backtracking for both the first and second tram waiting times.
class TripPlanner:
    def __init__(self, lines: List[Line], api: MetroAPI):
        self.lines = lines
        self.api = api
        logger.info(f"TripPlanner initialized with {len(lines)} lines")

    def get_first_tram_time(self, station: Station, line: Line) -> Optional[Tuple[int, int]]:
        """
        Returns a tuple (waiting_time, source_index) for the first upcoming tram at the candidate station.
        It uses the API result at the candidate station; if not valid, it backtracks until a valid time is found.
        """
        logger.debug(f"Getting first tram time for station {station.name} on line {line.line_code}")
        waiting_time = self.api.get_waiting_time(station, line.line_code)
        logger.debug(f"Direct API waiting time: {waiting_time}, Required walking time: {station.walking_time}")
        
        if waiting_time is not None and waiting_time >= station.walking_time:
            logger.info(f"Found valid first tram time: {waiting_time} minutes (from candidate station index: {station.index})")
            return (waiting_time, station.index)
        
        logger.debug("Direct time not valid, trying backtracking")
        return self.estimate_tram_time_from_backtracking(station, line)

    def estimate_tram_time_from_backtracking(self, station: Station, line: Line) -> Optional[Tuple[int, int]]:
        """
        Backtracks from the candidate station to earlier stations (one segment at a time)
        to calculate an effective waiting time at the candidate station.
        Returns a tuple (effective_time, source_index) if found.
        """
        logger.debug(f"Backtracking from station {station.name} (index: {station.index})")
        candidate_index = station.index
        
        for j in range(candidate_index - 1, -1, -1):
            current_station = line.stations[j]
            logger.debug(f"Checking station {current_station.name} (index: {j})")
            
            wt = self.api.get_waiting_time(current_station, line.line_code)
            logger.debug(f"Waiting time at station {current_station.name}: {wt}")
            
            if wt is None:
                continue
                
            effective_time = wt + (candidate_index - j) * line.travel_time_between_stations
            logger.debug(f"Calculated effective time: {effective_time} minutes")
            
            if effective_time >= station.walking_time:
                logger.info(f"Found valid time through backtracking: {effective_time} minutes (from station index: {j})")
                return (effective_time, j)
        
        logger.warning("No valid time found through backtracking")
        return None

    def get_second_tram_time(self, station: Station, line: Line, first_time: int, first_source_index: int) -> Optional[int]:
        """
        Computes the waiting time (in minutes) for the second upcoming tram at the candidate station.
        It backtracks further than the station used for the first tram (starting from first_source_index - 1)
        so that the effective waiting time is strictly greater than the first waiting time.
        """
        logger.debug(f"Getting second tram time for station {station.name} (first time: {first_time} from station index: {first_source_index})")
        candidate_index = station.index
        
        for j in range(first_source_index - 1, -1, -1):
            current_station = line.stations[j]
            logger.debug(f"Checking station {current_station.name} (index: {j})")
            
            wt = self.api.get_waiting_time(current_station, line.line_code)
            logger.debug(f"Waiting time at station {current_station.name}: {wt}")
            
            if wt is None:
                continue
                
            effective_time = wt + (candidate_index - j) * line.travel_time_between_stations
            logger.debug(f"Calculated effective time: {effective_time} minutes")
            
            if effective_time >= station.walking_time and effective_time > first_time:
                logger.info(f"Found valid second tram time: {effective_time} minutes")
                return effective_time
        
        logger.warning("No valid second tram time found")
        return None

    def get_feasible_tram_times(self, station: Station, line: Line) -> List[int]:
        """
        Returns a list with two waiting times for the candidate station:
          - The first upcoming tram (using direct API call or backtracking).
          - The second upcoming tram (calculated by further backtracking).
        If the second tram cannot be determined, only the first is returned.
        """
        logger.debug(f"Getting feasible tram times for station {station.name} on line {line.line_code}")
        first_result = self.get_first_tram_time(station, line)
        if first_result is None:
            logger.warning("No first tram time found")
            return []
            
        first_time, first_source_index = first_result
        logger.debug(f"First tram time: {first_time} minutes from station index: {first_source_index}")
        
        second = self.get_second_tram_time(station, line, first_time, first_source_index)
        logger.debug(f"Second tram time: {second}")
        
        if second is None:
            logger.info(f"Returning only first tram time: [{first_time}]")
            return [first_time]
            
        logger.info(f"Returning both tram times: [{first_time}, {second}]")
        return [first_time, second]

    def plan_trip(self) -> Dict[str, List[int]]:
        """
        For each line (candidate), retrieves the waiting times (first and second tram)
        and returns a mapping of "Station Name (Line Name, Direction)" to the list of waiting times.
        """
        logger.info("Planning trip for all lines")
        feasible_trams = {}
        
        for line in self.lines:
            logger.debug(f"Processing line {line.line_code}")
            candidate_station = next((s for s in line.stations if s.active), None)
            
            if candidate_station:
                logger.debug(f"Found candidate station: {candidate_station.name}")
                times = self.get_feasible_tram_times(candidate_station, line)
                
                if times:
                    key = f"{candidate_station.name} ({line.name}, Direction {line.direction})"
                    feasible_trams[key] = times
                    logger.info(f"Added feasible times for {key}: {times}")
                else:
                    logger.warning(f"No feasible times found for {candidate_station.name} on line {line.line_code}")
        
        logger.info(f"Trip planning complete. Found {len(feasible_trams)} feasible options")
        return feasible_trams

    def best_tram(self) -> Optional[Dict[str, any]]:
        """
        Compares the candidate stations across all lines and returns the best option,
        defined as the candidate with the minimum first waiting time.
        Returns a dictionary with keys: 'station_line' and 'waiting_times'.
        """
        best = None
        best_wait = float('inf')
        feasible = self.plan_trip()
        for key, times in feasible.items():
            if times and times[0] < best_wait:
                best_wait = times[0]
                best = {"station_line": key, "waiting_times": times}
        return best

def main():
    logger.info("Starting ATM trip planner")
    try:
        # Load lines from a separate JSON file.
        lines = load_lines_from_file("/Users/andre/Desktop/Coding/Python/tenv/Projects/atm/lines.json")
        
        # Candidate dictionary mapping line codes to the target station info.
        candidates = {
            "15": {"direction": "0", "target_station_code": "15371", "walking_time": 2},
            "3":  {"direction": "0", "target_station_code": "11139", "walking_time": 2}
        }
        logger.info(f"Using candidates configuration: {candidates}")
        
        # Update loaded lines with candidate (active) station info.
        update_lines_with_candidates(lines, candidates)
        
        # Instantiate the Metro API client.
        metro_api = MetroAPI()
        
        # Create the trip planner with the loaded lines.
        planner = TripPlanner(lines=lines, api=metro_api)
        
        # Get and display the feasible tram times for each candidate station.
        logger.info("Fetching trip plan")
        trip_plan = planner.plan_trip()
        print("\nFeasible tram times for candidate stations:")
        for station_line, times in trip_plan.items():
            print(f"{station_line}: {times} minutes")
        
        # Determine and display the best tram option.
        logger.info("Finding best tram option")
        best = planner.best_tram()
        if best:
            print("\nBest tram option:")
            print(f"{best['station_line']} with waiting times {best['waiting_times']} minutes")
            logger.info(f"Best option found: {best}")
        else:
            print("\nNo feasible tram option found.")
            logger.warning("No feasible tram option found")
            
    except Exception as e:
        logger.error(f"Fatal error in main: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()