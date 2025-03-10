import json
import re
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from curl_cffi import requests as curlq
import requests


def parse_wait_message(wait_message: str) -> Optional[int]:
    """Extract integer wait from "X min" or 'in arrivo' => 1, else None."""
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
    return Line(
        name=line_description,
        line_code=line_code,
        direction=direction,
        stations=stations_list
    )


def load_lines_from_file(filename: str) -> List[Line]:
    print(f"Loading lines from file: {filename}")
    with open(filename, "r", encoding="utf-8") as f:
        data = json.load(f)
    lines_data = data.get("lines", [])
    lines = [load_line_data(ld) for ld in lines_data]
    print(f"Successfully loaded {len(lines)} lines")
    return lines


def update_lines_with_candidates(lines: List[Line], candidates: Dict[str, Dict[str, Any]]):
    """
    Mark the station with 'target_station_code' as active on each line that matches the candidate's direction.
    """
    print("Updating lines with candidate information")
    for line in lines:
        c = candidates.get(line.line_code)
        if c and c.get("direction") == line.direction:
            tcode = c.get("target_station_code", "")
            wtime = c.get("walking_time", 7)
            for st in line.stations:
                if st.code == tcode:
                    st.active = True
                    st.walking_time = wtime
                    break


class MetroAPI:
    """
    Fetches the single "next tram" wait time from the ATM linesummary endpoint.
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
            print(f"Request error at station {station.name} code={station.code}: {e}")
            return None
        except json.JSONDecodeError as e:
            print(f"JSON parse error at station {station.name} code={station.code}: {e}")
            return None
        except Exception as e:
            print(f"Unexpected error at station {station.name} code={station.code}: {e}")
            return None


# --------------------------------------------------------------------------
# TRIP PLANNER
# --------------------------------------------------------------------------

class TripPlanner:
    """
    A trip planner that:
      1) Gathers raw waits from candidate_idx down to 0 in an array.
      2) Interprets None as 0 for "jump detection" (strictly greater => new tram).
      3) Finds up to TWO trams for each candidate station.
      4) For each tram, we compute arrival_time at the station, and see if it's feasible (>= walking_time).
      5) Summarizes all lines' trams and picks the best feasible arrival (lowest).
    """

    def __init__(self, lines: List[Line], api: MetroAPI):
        self.lines = lines
        self.api = api

    def _gather_raw_waits(self, line: Line, candidate_idx: int) -> List[Optional[int]]:
        """
        Returns an array of raw waits from station index=candidate_idx down to 0:
          raw_waits[0] => candidate station's wait
          raw_waits[1] => candidate_idx-1
          ...
          raw_waits[n-1] => station index=0
        """
        raw_waits = []
        for i in range(candidate_idx, -1, -1):
            st = line.stations[i]
            w = self.api.get_waiting_time(st, line.line_code)
            raw_waits.append(w)
        return raw_waits

    def _compute_arrival(self, line: Line, station_idx: int, candidate_idx: int, raw_wait: int) -> int:
        """arrival_time = raw_wait + (distance_in_stations * travel_time)."""
        dist = candidate_idx - station_idx
        return raw_wait + dist * line.travel_time_between_stations

    def find_two_trams(self, station: Station, line: Line) -> List[Dict[str, Any]]:
        """
        Returns up to two "tram" info dicts, each with:
          {
            "arrival": <minutes from now>,
            "feasible": <True/False>,
            "walk_time": station.walking_time,
            "wait_at_stop": <arrival - walk_time if feasible else None>,
            "raw_wait": the raw wait we used,
            "station_idx": which station provided that raw wait
          }
        So you can see both feasible and non-feasible trams.

        Steps:
         1) gather raw waits in array raw_waits[0..n-1]
         2) none => 0 for jump detection
         3) first station's raw wait => first tram
         4) if we see a strictly bigger raw wait => second tram
        """
        cidx = station.index
        walking_time = station.walking_time

        raw_waits = self._gather_raw_waits(line, cidx)
        print(f"\nRaw waits for line {line.line_code} (from station index {cidx} down to 0): {raw_waits}")

        # We'll store up to 2 trams in this list
        found_trams = []

        last_tram_wait = None
        for i, maybe_w in enumerate(raw_waits):
            # maybe_w can be None => interpret as 0 for jump detection
            numeric_w = maybe_w if maybe_w is not None else 0
            actual_station_idx = cidx - i

            if last_tram_wait is None:
                # first tram
                last_tram_wait = numeric_w
                found_trams.append({"raw_wait": numeric_w, "station_idx": actual_station_idx})
            else:
                if numeric_w > last_tram_wait:
                    # second tram
                    found_trams.append({"raw_wait": numeric_w, "station_idx": actual_station_idx})
                    break
                # else same or smaller => same tram, skip

            # We only want 2 total
            if len(found_trams) == 2:
                break

        # Now we convert raw_wait => arrival times, check feasibility
        results = []
        for tram_info in found_trams:
            w = tram_info["raw_wait"]
            st_idx = tram_info["station_idx"]
            arrival = self._compute_arrival(line, st_idx, cidx, w)
            feasible = (arrival >= walking_time)
            wait_at_stop = (arrival - walking_time) if feasible else None

            r = {
                "arrival": arrival,
                "feasible": feasible,
                "walk_time": walking_time,
                "wait_at_stop": wait_at_stop,
                "raw_wait": w,
                "station_idx": st_idx
            }
            results.append(r)

        return results

    def plan_trip(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        For each line with an active station, we gather up to two tram dicts from find_two_trams().
        Returns a dict: { "StationName (LineName, Dir X)": [ <tramInfo1>, <tramInfo2> ] }
        """
        out = {}
        for line in self.lines:
            cand = next((s for s in line.stations if s.active), None)
            if cand:
                tram_list = self.find_two_trams(cand, line)
                if tram_list:
                    key = f"{cand.name} ({line.name}, Direction {line.direction})"
                    out[key] = tram_list
        return out

    def best_tram(self) -> Optional[Dict[str, Any]]:
        """
        Among all lines, pick the tram with the smallest 'arrival' that is feasible.
        Returns e.g. {
          "station_line": <string key>,
          "tram": <tramInfo>
        } 
        or None if no feasible tram found.
        """
        feasible_options = []
        trip_plan = self.plan_trip()

        for station_line, tram_infos in trip_plan.items():
            for info in tram_infos:
                if info["feasible"]:
                    # gather all feasible
                    feasible_options.append((station_line, info))

        if not feasible_options:
            return None

        # pick the feasible arrival with min arrival
        feasible_options.sort(key=lambda x: x[1]["arrival"])
        best_station_line, best_tram_info = feasible_options[0]
        return {
            "station_line": best_station_line,
            "tram": best_tram_info
        }


def main():
    print("Starting ATM trip planner")

    # Adjust your JSON file path here
    lines_file = "/Users/andre/Desktop/Coding/Python/tenv/Projects/atm/lines.json"
    lines = load_lines_from_file(lines_file)

    # Example config
    candidates = {
        "15": {"direction": "0", "target_station_code": "15371", "walking_time": 8},
        "3":  {"direction": "0", "target_station_code": "11139", "walking_time": 4}
    }
    update_lines_with_candidates(lines, candidates)

    metro_api = MetroAPI()
    planner = TripPlanner(lines, metro_api)

    trip_plan = planner.plan_trip()

    print("\nFeasible tram times for candidate stations (showing up to 2 trams each):")
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
        total_arrival = tinfo["arrival"]
        walk_t = tinfo["walk_time"]
        wait_stop = tinfo["wait_at_stop"]
        print(f"\nBest tram option:\n{sl}")
        print(f" - Arrives in {total_arrival} minutes.")
        print(f" - Walking time is {walk_t} min => you'd wait at the stop {wait_stop} min before it arrives.")


if __name__ == "__main__":
    main()