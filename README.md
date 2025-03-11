# ATM Transit Planner

This project provides a real-time transit planning system for ATM (Azienda Trasporti Milanesi) in Milan, Italy. It helps users find the best tram/bus options based on their walking distance to various stops.

## Project Structure

### Core Files

- `final.py`: The main implementation of the transit planner with sequential processing. Contains core classes and logic for fetching and processing transit data.

- `final_threaded.py`: An optimized version of the transit planner that uses multi-threading to improve performance when fetching transit data.

- `final_sequential.py`: Another version of the transit planner that processes requests sequentially, useful for comparison and debugging.

### API and Testing

- `atm_api.py`: A Flask-based REST API that exposes the transit planner functionality. Provides endpoints for:

  - `/health`: Health check endpoint
  - `/plan`: Main trip planning endpoint
  - `/lines`: Get information about available transit lines

- `test_api.py`: Test suite for the API endpoints, includes tests for health check, line information retrieval, and trip planning.

### Data Management

- `line_summary.py`: Utility script for fetching and saving transit line data. Used to generate the `lines.json` file.

- `lines.json`: Contains the static data about transit lines, including:
  - Line codes and descriptions
  - Station names and codes
  - Station sequences and indices

## Key Features

- Real-time transit data fetching
- Multi-threaded processing for improved performance
- Intelligent trip planning based on walking times
- REST API for easy integration
- Support for multiple transit lines and directions
- Caching mechanism to improve response times

## Usage

1. Start the API server:

```bash
python atm_api.py
```

2. The API will be available at `http://localhost:3001`

3. To test the API:

```bash
python test_api.py
```

4. To update transit line data:

```bash
python line_summary.py
```

## Note

This project uses the ATM public API and requires an active internet connection to fetch real-time transit data.
