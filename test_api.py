import requests
import json
from typing import List, Dict, Any
from curl_cffi import requests as curlq
import time

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

def test_health() -> None:
    """Test the health check endpoint."""
    print("\n=== Testing Health Check Endpoint ===")
    response = requests.get("http://localhost:3000/health")
    print(f"Status Code: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")

def test_get_lines() -> None:
    """Test the get lines endpoint."""
    print("\n=== Testing Get Lines Endpoint ===")
    response = requests.get("http://localhost:3000/lines")
    print(f"Status Code: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")

def test_plan_trip() -> None:
    """Test the plan trip endpoint with sample candidates."""
    print("\n=== Testing Plan Trip Endpoint ===")
    
    # Sample candidates data
    candidates = [
        {
            "line_code": "15",
            "direction": "0",
            "target_station_code": "15371",
            "walking_time": 8
        },
        {
            "line_code": "3",
            "direction": "0",
            "target_station_code": "11139",
            "walking_time": 4
        },
        {
            "line_code": "59",
            "direction": "0",
            "target_station_code": "11154",
            "walking_time": 8
        }
    ]
    
    payload = {
        "candidates": candidates
    }
    
    # Make the POST request
    response = requests.post(
        "http://localhost:3000/plan",
        json=payload,
        headers={"Content-Type": "application/json"}
    )
    
    print(f"Status Code: {response.status_code}")
    if response.status_code == 200:
        result = response.json()
        
        # Print raw JSON response
        print("\n=== Raw JSON Response ===")
        print(json.dumps(result, indent=2))
        
        print("\n=== Formatted Output ===")
        print("Execution Time:", result.get("execution_time"), "seconds")
        
        # Print best option if available
        best = result.get("best_option")
        if best:
            print("\nBest Option:")
            print(f"Station/Line: {best['station_line']}")
            print(f"Arrival in: {best['arrival']} minutes")
            print(f"Walking time: {best['walk_time']} minutes")
            print(f"Wait at stop: {best['wait_at_stop']} minutes")
        
        # Print all feasible options
        print("\nAll Feasible Options:")
        for station_line, trams in result.get("feasible_options", {}).items():
            print(f"\n{station_line}:")
            for i, tram in enumerate(trams, 1):
                if tram["feasible"]:
                    print(f"  Tram #{i}:")
                    print(f"    Arrival: {tram['arrival']} minutes")
                    print(f"    Walking time: {tram['walk_time']} minutes")
                    print(f"    Wait at stop: {tram['wait_at_stop']} minutes")
                    print(f"    Raw wait: {tram['raw_wait']} minutes")
    else:
        print("Error Response:", json.dumps(response.json(), indent=2))

def main():
    """Run all API tests."""
    try:
        # Test health endpoint
        test_health()
        time.sleep(1)  # Small delay between requests
        
        # Test get lines endpoint
        
          # Small delay between requests
        
        # Test plan trip endpoint
        test_plan_trip()
        
    except requests.exceptions.ConnectionError:
        print("\nError: Could not connect to the API server.")
        print("Make sure the Flask API server is running (python atm_api.py)")
    except Exception as e:
        print(f"\nError during testing: {str(e)}")

if __name__ == "__main__":
    main()
