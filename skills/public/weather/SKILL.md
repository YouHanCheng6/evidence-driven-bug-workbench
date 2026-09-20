---
name: weather
description: Use this skill when the user asks for current weather, a weather forecast, temperature, precipitation, humidity, wind, or weather advice for a named city, district, or airport. Ask for a location when it is missing; never infer it from the user's IP address.
---

# Weather

Use this skill for weather questions only.

## Location first

- A city, district, or airport code is required.
- If the location is absent or ambiguous, ask exactly one concise question, such as: “你想查询哪个城市或区县的天气？”
- Never use IP-based location or silently default to Beijing or another city.

## Retrieve current conditions and forecast

1. Prefer the available web-fetch tool with an URL-encoded location:
   `https://wttr.in/<location>?format=j1`
2. If web fetch is unavailable, use a bounded terminal request to the same URL (`curl --max-time 12`).
3. Read only the fields needed for the request. For a current-weather question, report condition, temperature, feels-like temperature, humidity, and wind. For a forecast, report the requested date plus high/low temperature and precipitation chance.
4. State the location and observation/forecast date. Do not invent unavailable values. If the service fails, explain that the weather service is temporarily unavailable and offer to retry.

## Response style

- Keep ordinary weather replies short and practical.
- Clearly distinguish current observations from forecasts.
- Give safety suggestions only when the data supports them (for example, rain gear for a high rain probability or heat precautions for high temperatures).
