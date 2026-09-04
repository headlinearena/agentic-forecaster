# FRED API Basics

## Base URL

```
https://api.stlouisfed.org/fred/
```

For GeoFRED endpoints:
```
https://api.stlouisfed.org/geofred/
```

## Authentication

All requests require an API key passed as a query parameter:

```
api_key=YOUR_32_CHARACTER_KEY
```

### Obtaining an API Key

1. Create account at https://fredaccount.stlouisfed.org
2. Log in and request an API key
3. Key is a 32-character lowercase alphanumeric string

### Rate Limits

- API implements rate limiting
- HTTP 429 (Too Many Requests) when exceeded
- Contact FRED team for higher limits if needed

## Response Formats

All endpoints support multiple formats via `file_type` parameter:

| Format | Content-Type |
|--------|--------------|
| `xml` | text/xml (default) |
| `json` | application/json |

Some observation endpoints also support:
- `csv` - Comma-separated values
- `xlsx` - Excel format

## Common Parameters

These parameters are available on most endpoints:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `api_key` | string | required | 32-character API key |
| `file_type` | string | xml | Response format: xml, json |
| `realtime_start` | date | today | Start of real-time period (YYYY-MM-DD) |
| `realtime_end` | date | today | End of real-time period (YYYY-MM-DD) |

## Real-Time Periods (ALFRED)

FRED supports historical (vintage) data access through real-time parameters.

## Pagination

Many endpoints support `limit` and `offset`.

## Sorting

Many endpoints support `order_by` and `sort_order`.

## Error Responses

**JSON:**
```json
{
    "error_code": 400,
    "error_message": "Bad Request. The value for variable api_key is not registered..."
}
```

## Data Types

Missing values in observation data are represented as `.`.
