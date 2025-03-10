import requests
import json
from typing import List, Dict
from curl_cffi import requests as curlq

def get_journey_pattern(line_code: str, direction: str = "0") -> Dict:
    """
    Fetch journey pattern data for a given line and direction.
    
    Args:
        line_code (str): The line code (e.g., '15')
        direction (str): The direction code (default '0')
        
    Returns:
        Dict: A dictionary containing line info and indexed station data
    """
    base_url = "https://giromilano.atm.it/proxy.tpportal/api/tpPortal"
    url = f"{base_url}/tpl/journeyPatterns/{line_code}%7C{direction}"
    
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
        
        # Create structured response
        result = {
            "line": {
                "code": data.get("Code"),
                "description": data.get("Line", {}).get("LineDescription")
            },
            "direction": data.get("Direction"),
            "stations": []
        }
        
        # Extract stations information with index
        if "Stops" in data:
            for idx, stop in enumerate(data["Stops"]):
                station = {
                    "index": idx,
                    "name": stop["Description"].strip(),
                    "code": stop["Code"]
                }
                result["stations"].append(station)
                
        return result
        
    except requests.RequestException as e:
        print(f"Error fetching data: {e}")
        return {}
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON response: {e}")
        print("Response content:", response.text)
        return {}

def main():
    lines_data = []
    
    while True:
        line_code = input("\nEnter line code (e.g., 15) or press Enter to finish: ").strip()
        if not line_code:
            break
            
        # Get directions for this line
        while True:
            direction = input(f"Enter direction for line {line_code} (0 or 1, or press Enter to move to next line): ").strip()
            if not direction:
                break
                
            if direction not in ['0', '1']:
                print("Invalid direction. Please enter 0 or 1.")
                continue
                
            print(f"Fetching data for line {line_code} direction {direction}...")
            result = get_journey_pattern(line_code, direction)
            
            if result:
                lines_data.append(result)
                print(f"✓ Added line {line_code} direction {direction}")
            else:
                print(f"✗ Failed to get data for line {line_code} direction {direction}")
    
    if lines_data:
        # Save to file
        output_file = "lines.json"
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump({"lines": lines_data}, f, indent=2, ensure_ascii=False)
            print(f"\nSuccessfully saved data for {len(lines_data)} line configurations to {output_file}")
        except IOError as e:
            print(f"Error saving to file: {e}")
    else:
        print("\nNo data was collected.")

if __name__ == "__main__":
    main() 