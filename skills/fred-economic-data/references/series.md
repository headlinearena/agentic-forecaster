# FRED Series Endpoints

Series endpoints provide access to economic data series metadata and observations.

## fred/series/observations

**URL:** `https://api.stlouisfed.org/fred/series/observations`

### Required Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `api_key` | string | 32-character API key |
| `series_id` | string | Series identifier |

### Useful Optional Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `file_type` | string | xml | xml, json, xlsx, csv |
| `limit` | integer | 100000 | 1-100000 |
| `sort_order` | string | asc | asc, desc |
| `observation_start` | date | 1776-07-04 | Filter start date |
| `observation_end` | date | 9999-12-31 | Filter end date |
| `units` | string | lin | Data transformation |
| `frequency` | string | none | Frequency aggregation |
| `aggregation_method` | string | avg | avg, sum, eop |

### Units Transformation Options

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

### Frequency Codes

| Code | Frequency |
|------|-----------|
| `d` | Daily |
| `w` | Weekly |
| `bw` | Biweekly |
| `m` | Monthly |
| `q` | Quarterly |
| `sa` | Semiannual |
| `a` | Annual |

### Response Shape

```json
{
  "sort_order": "asc",
  "observations": [
    {
      "date": "2020-01-01",
      "value": "1.1"
    }
  ]
}
```
