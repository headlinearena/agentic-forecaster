---
name: fred-economic-data
description: Query FRED (Federal Reserve Economic Data) API for 800,000+ economic time series from 100+ sources. Access GDP, unemployment, inflation, interest rates, exchange rates, housing, and regional data. Use for macroeconomic analysis, financial research, policy studies, economic forecasting, and academic research requiring U.S. and international economic indicators.
license: Unknown
metadata:
    skill-author: K-Dense Inc.
---

# FRED Economic Data Access

## Overview

Access comprehensive economic data through FRED (Federal Reserve Economic Data), a database maintained by the Federal Reserve Bank of St. Louis containing over 800,000 economic time series from over 100 sources.

**Key capabilities:**
- Query economic time series data (GDP, unemployment, inflation, interest rates)
- Search and discover series by keywords, tags, and categories
- Access historical data and vintage (revision) data via ALFRED
- Retrieve release schedules and data publication dates
- Map regional economic data with GeoFRED
- Apply data transformations (percent change, log, etc.)

## API Key Setup

**Required:** All FRED API requests require an API key.

1. Create an account at https://fredaccount.stlouisfed.org
2. Log in and request an API key through the account portal
3. Set as environment variable:

```bash
export FRED_API_KEY="your_32_character_key_here"
```

Or in Python:
```python
import os
os.environ["FRED_API_KEY"] = "your_key_here"
```

## Quick Start

### Using the FREDQuery Class

```python
from scripts.fred_query import FREDQuery

# Initialize with API key
fred = FREDQuery(api_key="YOUR_KEY")  # or uses FRED_API_KEY env var

# Get GDP data
gdp = fred.get_series("GDP")
print(f"Latest GDP: {gdp['observations'][-1]}")

# Get unemployment rate observations
unemployment = fred.get_observations("UNRATE", limit=12)
for obs in unemployment["observations"]:
    print(f"{obs['date']}: {obs['value']}%")

# Search for inflation series
inflation_series = fred.search_series("consumer price index")
for s in inflation_series["seriess"][:5]:
    print(f"{s['id']}: {s['title']}")
```

### Direct API Calls

```python
import requests
import os

API_KEY = os.environ.get("FRED_API_KEY")
BASE_URL = "https://api.stlouisfed.org/fred"

# Get series observations
response = requests.get(
    f"{BASE_URL}/series/observations",
    params={
        "api_key": API_KEY,
        "series_id": "GDP",
        "file_type": "json"
    }
)
data = response.json()
```

## Popular Economic Series

| Series ID | Description | Frequency |
|-----------|-------------|-----------|
| GDP | Gross Domestic Product | Quarterly |
| GDPC1 | Real Gross Domestic Product | Quarterly |
| UNRATE | Unemployment Rate | Monthly |
| CPIAUCSL | Consumer Price Index (All Urban) | Monthly |
| FEDFUNDS | Federal Funds Effective Rate | Monthly |
| DGS10 | 10-Year Treasury Constant Maturity | Daily |
| HOUST | Housing Starts | Monthly |
| PAYEMS | Total Nonfarm Payrolls | Monthly |
| INDPRO | Industrial Production Index | Monthly |
| M2SL | M2 Money Stock | Monthly |
| UMCSENT | Consumer Sentiment | Monthly |
| SP500 | S&P 500 | Daily |

## API Endpoint Categories

### Series Endpoints

Get economic data series metadata and observations.

**Key endpoints:**
- `fred/series` - Get series metadata
- `fred/series/observations` - Get data values (most commonly used)
- `fred/series/search` - Search for series by keywords
- `fred/series/updates` - Get recently updated series

```python
# Get observations with transformations
obs = fred.get_observations(
    series_id="GDP",
    units="pch",  # percent change
    frequency="q",  # quarterly
    observation_start="2020-01-01"
)

# Search with filters
results = fred.search_series(
    "unemployment",
    filter_variable="frequency",
    filter_value="Monthly"
)
```

**Reference:** See `references/series.md` for all 10 series endpoints

### Categories Endpoints

Navigate the hierarchical organization of economic data.

**Key endpoints:**
- `fred/category` - Get a category
- `fred/category/children` - Get subcategories
- `fred/category/series` - Get series in a category

**Reference:** See `references/categories.md` for all 6 category endpoints

### Releases Endpoints

Access data release schedules and publication information.

**Key endpoints:**
- `fred/releases` - Get all releases
- `fred/releases/dates` - Get upcoming release dates
- `fred/release/series` - Get series in a release

**Reference:** See `references/releases.md` for all 9 release endpoints

### Tags Endpoints

Discover and filter series using FRED tags.

**Reference:** See `references/tags.md` for all 3 tag endpoints

### Sources Endpoints

Get information about data sources (BLS, BEA, Census, etc.).

**Reference:** See `references/sources.md` for all 3 source endpoints

### GeoFRED Endpoints

Access geographic/regional economic data for mapping.

**Reference:** See `references/geofred.md` for all 4 GeoFRED endpoints

## Data Transformations

Apply transformations when fetching observations:

| Value | Description |
|-------|-------------|
| `lin` | Levels (no transformation) |
| `chg` | Change from previous period |
| `ch1` | Change from year ago |
| `pch` | Percent change from previous period |
| `pc1` | Percent change from year ago |
| `pca` | Compounded annual rate of change |
| `cch` | Continuously compounded rate of change |
| `cca` | Continuously compounded annual rate of change |
| `log` | Natural log |

## Frequency Aggregation

Aggregate data to different frequencies:

| Code | Frequency |
|------|-----------|
| `d` | Daily |
| `w` | Weekly |
| `m` | Monthly |
| `q` | Quarterly |
| `a` | Annual |

Aggregation methods: `avg` (average), `sum`, `eop` (end of period)

## Real-Time (Vintage) Data

Access historical vintages of data via ALFRED by setting `realtime_start` and `realtime_end`.

## Error Handling

Missing values in observations are represented as `"."`; callers should skip them when coercing to numbers.

## Rate Limits

- API implements rate limiting
- HTTP 429 returned when exceeded
- Use caching for frequently accessed data

## Reference Documentation

For detailed endpoint documentation:
- **Series endpoints** - See `references/series.md`
- **API basics** - See `references/api_basics.md`

## Additional Resources

- **FRED Homepage**: https://fred.stlouisfed.org/
- **API Documentation**: https://fred.stlouisfed.org/docs/api/fred/
- **GeoFRED Maps**: https://geofred.stlouisfed.org/
- **ALFRED (Vintage Data)**: https://alfred.stlouisfed.org/
- **Terms of Use**: https://fred.stlouisfed.org/legal/
