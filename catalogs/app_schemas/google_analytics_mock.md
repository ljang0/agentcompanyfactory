# google_analytics_mock Schema

**Deploy order**: 35 (alphabetical among all `*_mock` directories; `BASE_PORT=8000` → port 8035)  
**Base URL**: `http://172.17.46.46:8035/`  
**Go Endpoint**: `GET /go?sid=<sid>` → `{initial_state, current_state, state_diff}`  
**Inject**: `POST /post?sid=<sid>` with body `{"action":"set","state":{...}}`

## State Schema

| Key | Type | Description and default |
|-----|------|-------------------------|
| `property` | `Property` | Default GA4 property for Acme Store |
| `dailyMetrics` | `Record<string, DailyMetric>` | Generated date-keyed metrics |
| `trafficSources` | `TrafficSource[]` | Generated acquisition rows |
| `events` | `Event[]` | Default analytics events |
| `pages` | `PageMetric[]` | Default page metrics |
| `countries` | `CountryMetric[]` | Default country metrics |
| `cities` | `CityMetric[]` | Default city metrics |
| `languages` | `LanguageMetric[]` | Six defaults |
| `ageBrackets` | `AgeMetric[]` | Six defaults |
| `genders` | `GenderMetric[]` | Three defaults |
| `techPlatforms` | `TechPlatforms` | Browser, OS, device, and resolution breakdowns |
| `realtimeData` | `RealtimeData` | Current real-time metrics |
| `retentionCohorts` | `RetentionCohort[]` | Eight defaults |
| `explorations` | `Exploration[]` | Three saved explorations |
| `customDimensions` | `CustomDimension[]` | Two defaults |
| `customMetrics` | `CustomMetric[]` | One default |
| `conversions` | `Conversion[]` | Four defaults |
| `audiences` | `Audience[]` | Five defaults |
| `selectedDateRange` | `DateRange` | `{preset:"last28days", startDate:"2024-11-18", endDate:"2024-12-15", compareEnabled:false, compareType:"precedingPeriod"}` |
| `activeComparison` | `object \| null` | Default: `null` |
| `recentlyAccessed` | `RecentItem[]` | Default: `[]` |
| `currentUser` | `CurrentUser` | Current analytics user |

### Entity fields

- Property: `{ propertyId, propertyName, accountName, accountId, websiteUrl, industry, timezone, currency, dataStreams[], createdAt }`
- DataStream: `{ id, name, type, url, measurementId, status, enhancedMeasurement }`; enhanced measurement: `{pageViews,scrolls,outboundClicks,siteSearch,formInteractions,videoEngagement,fileDownloads}`
- DailyMetric: `{ date, users, newUsers, returningUsers, sessions, engagedSessions, engagementRate, avgSessionDuration, avgEngagementTime, sessionsPerUser, screenPageViews, viewsPerSession, eventCount, conversions, totalRevenue, purchaseRevenue, ecommercePurchases, bounceRate }`
- TrafficSource: `{ id, channelGroup, source, medium, campaign, users, newUsers, sessions, engagedSessions, engagementRate, avgEngagementTime, eventCount, conversions, totalRevenue }`
- Event: `{ id, name, count, totalUsers, countPerUser, isKeyEvent, revenue }`
- PageMetric: `{ id, pagePath, pageTitle, views, users, viewsPerUser, avgEngagementTime, eventCount, conversions }`
- CountryMetric: `{ id, country, countryCode, users, newUsers, sessions, engagementRate, avgEngagementTime, conversions, revenue }`; CityMetric: `{id,city,country,users,sessions}`
- LanguageMetric: `{language,users,percentage}`; AgeMetric: `{bracket,users,percentage}`; GenderMetric: `{gender,users,percentage}`
- TechPlatforms: `{ browsers[], operatingSystems[], devices[], screenResolutions[] }`; rows use `{name,users,percentage}`, except resolution rows use `resolution` instead of `name`
- RealtimeData: `{ activeUsers, usersPerMinute[], byCountry[], bySource[], byPage[], byDevice[] }`
- RetentionCohort: `{ cohortDate, cohortSize, retention[] }`
- Exploration: `{ id, name, type, createdAt, lastModified, owner, shared, config }`
- CustomDimension: `{ id, name, scope, description, parameterName }`; CustomMetric adds `unit`
- Conversion: `{ id, eventName, isKeyEvent, createdAt, count, value }`
- Audience: `{ id, name, description, membershipDuration, userCount, trigger }`
- DateRange: `{ preset, startDate, endDate, compareEnabled, compareType }`; RecentItem: `{path,title,timestamp}`; CurrentUser: `{name,email,role,avatarUrl}`

## Minimal Inject Example

```json
{
  "type": "chrome_open_url",
  "parameters": {
    "url": "http://172.17.46.46:8035/",
    "inject_state": true,
    "state_content": {
      "action": "set",
      "state": {
        "selectedDateRange": {"preset":"custom","startDate":"2026-03-01","endDate":"2026-03-16","compareEnabled":false,"compareType":"previous_period"},
        "recentlyAccessed": []
      }
    }
  }
}
```

The app deep-merges injected objects with the generated defaults.

## Observable State Changes (for LLM evaluation)

| User Action | State Field Changed |
|-------------|---------------------|
| Apply a preset/custom date range or comparison | `selectedDateRange` |
| Navigate to a tracked report/admin page | `recentlyAccessed` (deduplicated item prepended, capped at 10) |
| Save property settings | `property.propertyName`, `property.industry`, `property.timezone`, `property.currency` |
| Toggle an enhanced-measurement option | `property.dataStreams[*].enhancedMeasurement.<field>` |
| Toggle an event as a key event | `events[*].isKeyEvent` |
| Create a custom dimension | `customDimensions` (new item appended) |
| Create a custom metric | `customMetrics` (new item appended) |
