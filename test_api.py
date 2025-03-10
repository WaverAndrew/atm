import requests
import json
from typing import List, Dict
from curl_cffi import requests as curlq

def get_station_wait_times(station_code: str) -> List[Dict[str, str]]:
    """
    Fetch wait times for a given station code from the ATM API.
    
    Args:
        station_code (str): The station code to query
        
    Returns:
        List[Dict[str, str]]: A list of dictionaries containing line codes and their wait times
    """
    base_url = "https://giromilano.atm.it/proxy.tpportal/api/tpPortal"
    url = f"{base_url}/tpl/stops/{station_code}/linesummary"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9,it;q=0.8',
        'Origin': 'https://giromilano.atm.it',
        'Referer': 'https://giromilano.atm.it/'
    }
    
    try:
        response = curlq.get(url, headers=headers, impersonate="chrome")
        response.raise_for_status()
        
        data = response.json()
        
        # Extract stop information
        stop_info = data.get("StopPoint", {})
        stop_name = stop_info.get("Description", "Unknown Stop")
        
        # Extract wait times for all lines
        wait_times = []
        for line in data.get("Lines", []):
            line_info = line.get("Line", {})
            wait_times.append({
                "stop_name": stop_name,
                "line_code": line_info.get("LineCode", ""),
                "line_description": line_info.get("LineDescription", ""),
                "wait_message": line.get("WaitMessage", "N/A"),
                "direction": line.get("Direction", ""),
                "journey_pattern_id": line.get("JourneyPatternId", ""),
                "transport_mode": "Tram" if line_info.get("TransportMode") == 0 else "Unknown",
                "is_suburban": line_info.get("Suburban", False)
            })
            
        return wait_times
        
    except requests.RequestException as e:
        print(f"Error fetching data: {e}")
        return []
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON response: {e}")
        print("Response content:", response.text)
        return []

def main():
    # Example usage
    station_code = input("Enter station code (e.g., 15371): ")
    wait_times = get_station_wait_times(station_code)
    
    if wait_times:
        print("\nCurrent wait times:")
        for info in wait_times:
            print(f"\nStop: {info['stop_name']}")
            print(f"Line {info['line_code']} - {info['transport_mode']}")
            print(f"Route: {info['line_description']}")
            print(f"Wait time: {info['wait_message']}")
            print(f"Direction: {info['direction']}")
            if info['is_suburban']:
                print("(Suburban line)")
    else:
        print("No wait times available for this station.")

if __name__ == "__main__":
    main()
