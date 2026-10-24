"""
AstrBot Weather Plugin
支持 wttr.in（免费，无需 API Key）和彩云天气（需要 API Key）
支持当前天气和未来天气预报
"""

from typing import Optional, List
import aiohttp
import json
import traceback
from datetime import datetime, timedelta

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


# 彩云天气天气现象映射
CAIYUN_SKYCON_MAP = {
    "CLEAR_DAY": "晴",
    "CLEAR_NIGHT": "晴（夜间）",
    "PARTLY_CLOUDY_DAY": "多云",
    "PARTLY_CLOUDY_NIGHT": "多云（夜间）",
    "CLOUDY": "阴",
    "LIGHT_HAZE": "轻度雾霾",
    "MODERATE_HAZE": "中度雾霾",
    "HEAVY_HAZE": "重度雾霾",
    "LIGHT_RAIN": "小雨",
    "MODERATE_RAIN": "中雨",
    "HEAVY_RAIN": "大雨",
    "STORM_RAIN": "暴雨",
    "FOG": "雾",
    "LIGHT_SNOW": "小雪",
    "MODERATE_SNOW": "中雪",
    "HEAVY_SNOW": "大雪",
    "STORM_SNOW": "暴雪",
    "DUST": "浮尘",
    "SAND": "沙尘",
    "WIND": "大风",
}


@register(
    "astrbot_plugin_weather_counhopig",
    "counhopig",
    "AstrBot 天气查询插件，支持 wttr.in 和彩云天气",
    "2.0.0"
)
class WeatherPlugin(Star):
    """Weather plugin for AstrBot"""
    
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        
        # 天气提供商：wttr 或 caiyun
        self.provider = self.config.get("weather_provider", "wttr")
        
        # 彩云天气配置
        self.caiyun_api_key = self.config.get("caiyun_api_key", "")
        self.caiyun_api_version = self.config.get("caiyun_api_version", "v2.6")
        
        # wttr.in 配置
        self.wttr_base_url = "https://wttr.in"
    
    async def initialize(self):
        """Initialize hook - called when plugin loads"""
        logger.info(f"天气插件已加载，当前提供商: {self.provider}")
        if self.provider == "caiyun" and not self.caiyun_api_key:
            logger.warning("已选择彩云天气但未配置 API Key，将回退到 wttr.in")
            self.provider = "wttr"
    
    # ==================== 命令接口 ====================
    
    @filter.command("weather", alias={"天气", "查天气", "tq"})
    async def query_weather(self, event: AstrMessageEvent):
        """
        Query weather for a city
        Usage: /weather <city> [days|明天|后天] or /天气 <城市名> [天数]
        """
        message_str = event.message_str.strip()
        parts = message_str.split(maxsplit=2)
        
        if len(parts) < 2:
            provider_name = "彩云天气" if self.provider == "caiyun" else "wttr.in"
            yield event.plain_result(
                f"请输入城市名称\n"
                f"当前提供商: {provider_name}\n"
                f"使用方法: /weather <城市名> [天数/明天/后天]\n"
                f"示例: /weather Beijing 或 /天气 北京 3 或 /天气 北京 明天"
            )
            return
        
        city = parts[1].strip()
        days = 0
        
        if len(parts) >= 3:
            days = self._parse_days(parts[2].strip())
        
        if days > 0:
            result = await self._do_fetch_forecast(city, days)
            yield event.plain_result(result)
        else:
            result = await self._do_fetch_weather(city)
            yield event.plain_result(result)
    
    @filter.command("forecast", alias={"天气预报", "未来天气", "yltq"})
    async def query_forecast(self, event: AstrMessageEvent):
        """
        Query future weather forecast for a city
        Usage: /forecast <city> [days] or /天气预报 <城市名> [天数]
        """
        message_str = event.message_str.strip()
        parts = message_str.split(maxsplit=2)
        
        if len(parts) < 2:
            yield event.plain_result(
                "请输入城市名称\n"
                "使用方法: /forecast <城市名> [天数] 或 /天气预报 <城市名> [天数]\n"
                "示例: /forecast Beijing 3 或 /天气预报 上海 3"
            )
            return
        
        city = parts[1].strip()
        days = 3  # Default to 3 days
        
        if len(parts) >= 3:
            parsed = self._parse_days(parts[2].strip())
            if parsed > 0:
                days = parsed
        
        result = await self._do_fetch_forecast(city, days)
        yield event.plain_result(result)
    
    @filter.command("setweather", alias={"设置天气", "天气源"})
    async def set_weather_provider(self, event: AstrMessageEvent):
        """
        设置天气数据提供商
        Usage: /setweather <wttr|caiyun> [api_key]
        """
        message_str = event.message_str.strip()
        parts = message_str.split(maxsplit=2)
        
        if len(parts) < 2:
            current = "彩云天气" if self.provider == "caiyun" else "wttr.in"
            yield event.plain_result(
                f"当前天气提供商: {current}\n\n"
                "使用方法:\n"
                "  /setweather wttr     - 切换到 wttr.in（免费，无需API Key）\n"
                "  /setweather caiyun <api_key>  - 切换到彩云天气（需要API Key）\n\n"
                "获取彩云天气API Key: https://dashboard.caiyunapp.com/"
            )
            return
        
        provider = parts[1].strip().lower()
        
        if provider == "wttr":
            self.provider = "wttr"
            self.config["weather_provider"] = "wttr"
            if hasattr(self.config, 'save_config'):
                self.config.save_config()
            yield event.plain_result("已切换到 wttr.in（免费天气服务）")
        
        elif provider == "caiyun":
            if len(parts) < 3:
                yield event.plain_result(
                    "请提供彩云天气 API Key\n"
                    "使用方法: /setweather caiyun <api_key>\n\n"
                    "获取API Key: https://dashboard.caiyunapp.com/"
                )
                return
            
            api_key = parts[2].strip()
            self.provider = "caiyun"
            self.caiyun_api_key = api_key
            self.config["weather_provider"] = "caiyun"
            self.config["caiyun_api_key"] = api_key
            if hasattr(self.config, 'save_config'):
                self.config.save_config()
            yield event.plain_result("已切换到彩云天气，API Key 已保存")
        
        else:
            yield event.plain_result(
                "无效的提供商，请选择 wttr 或 caiyun\n"
                "使用方法: /setweather <wttr|caiyun> [api_key]"
            )
    
    @filter.llm_tool(name="get_weather")
    async def get_weather_tool(self, event: AstrMessageEvent, location: str, days: int = 0):
        '''获取指定城市的当前天气或未来天气预报信息。

        Args:
            location(string): 城市名称，例如"北京"、"上海"、"香港"
            days(int): 查询未来几天的天气，0表示当前天气，1表示明天，2表示后天，3表示大后天。默认为0。
        '''
        if days and days > 0:
            return await self._do_fetch_forecast(location, days)
        else:
            return await self._do_fetch_weather(location)
    
    # ==================== 统一数据获取接口 ====================
    
    async def _do_fetch_weather(self, city: str) -> str:
        """统一的天气获取接口，根据 provider 选择数据源"""
        try:
            if self.provider == "caiyun" and self.caiyun_api_key:
                data = await self._fetch_caiyun_weather(city)
                if data:
                    return self._format_caiyun_weather(data)
                # 彩云失败，回退到 wttr
                logger.warning(f"彩云天气获取失败，回退到 wttr.in: {city}")
            
            data = await self._fetch_wttr_weather(city)
            if data:
                return self._format_wttr_weather(data)
            
            return f"抱歉，无法获取 {city} 的天气信息。\n可能原因：城市名称错误或天气服务暂时不可用。"
        except Exception as e:
            logger.error(f"获取天气失败: {e}")
            return f"获取天气时发生错误: {str(e)}"
    
    async def _do_fetch_forecast(self, city: str, days: int) -> str:
        """统一的预报获取接口，根据 provider 选择数据源"""
        try:
            if self.provider == "caiyun" and self.caiyun_api_key:
                data = await self._fetch_caiyun_forecast(city, days)
                if data:
                    return self._format_caiyun_forecast(data)
                # 彩云失败，回退到 wttr
                logger.warning(f"彩云天气预报获取失败，回退到 wttr.in: {city}")
            
            data = await self._fetch_wttr_forecast(city, days)
            if data:
                return self._format_wttr_forecast(data)
            
            return f"抱歉，无法获取 {city} 的天气预报。\n可能原因：城市名称错误或天气服务暂时不可用。"
        except Exception as e:
            logger.error(f"获取天气预报失败: {e}")
            return f"获取天气预报时发生错误: {str(e)}"
    
    # ==================== 彩云天气 API ====================
    
    async def _geocode_city(self, city: str) -> Optional[tuple]:
        """
        使用 Nominatim 将城市名转换为经纬度
        Returns: (longitude, latitude) or None
        """
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": city,
            "format": "json",
            "limit": 1,
            "accept-language": "zh-CN,zh,en"
        }
        headers = {
            "User-Agent": "AstrBot-Weather-Plugin/2.0"
        }
        
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.warning(f"Geocoding API returned status {resp.status}")
                        return None
                    
                    data = await resp.json()
                    if not data:
                        logger.warning(f"No geocoding result for: {city}")
                        return None
                    
                    lon = float(data[0]["lon"])
                    lat = float(data[0]["lat"])
                    return (lon, lat)
        except Exception as e:
            logger.error(f"Geocoding error for {city}: {e}")
            return None
    
    async def _fetch_caiyun_api(self, city: str, endpoint: str = "realtime") -> Optional[dict]:
        """调用彩云天气 API"""
        coords = await self._geocode_city(city)
        if not coords:
            logger.warning(f"无法获取 {city} 的坐标")
            return None
        
        lon, lat = coords
        version = self.caiyun_api_version or "v2.6"
        url = f"https://api.caiyunapp.com/{version}/{self.caiyun_api_key}/{lon},{lat}/{endpoint}"
        
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            headers = {
                "User-Agent": "AstrBot-Weather-Plugin/2.0",
                "Accept": "application/json"
            }
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"Caiyun API returned status {resp.status}")
                        return None
                    
                    data = await resp.json()
                    if data.get("status") != "ok":
                        logger.warning(f"Caiyun API error: {data.get('status')}")
                        return None
                    
                    return data
        except Exception as e:
            logger.error(f"Caiyun API error for {city}: {e}")
            return None
    
    async def _fetch_caiyun_weather(self, city: str) -> Optional[dict]:
        """获取彩云天气实时数据"""
        data = await self._fetch_caiyun_api(city, "realtime")
        if not data:
            return None
        
        realtime = data.get("result", {}).get("realtime", {})
        location = data.get("location", [])
        
        # 获取城市名（从 API 响应）
        city_name = city
        
        return {
            "city": city_name,
            "location": location,
            "realtime": realtime
        }
    
    async def _fetch_caiyun_forecast(self, city: str, days: int) -> Optional[dict]:
        """获取彩云天气预报数据"""
        data = await self._fetch_caiyun_api(city, "daily")
        if not data:
            return None
        
        daily = data.get("result", {}).get("daily", {})
        location = data.get("location", [])
        
        return {
            "city": city,
            "location": location,
            "days": days,
            "daily": daily
        }
    
    def _format_caiyun_weather(self, data: dict) -> str:
        """格式化彩云天气实时数据"""
        realtime = data.get("realtime", {})
        city = data.get("city", "未知")
        
        temperature = realtime.get("temperature", "--")
        apparent_temp = realtime.get("apparent_temperature", "--")
        humidity = realtime.get("humidity", "--")
        skycon = realtime.get("skycon", "UNKNOWN")
        visibility = realtime.get("visibility", "--")
        pressure = realtime.get("pressure", "--")
        
        # 转换湿度为百分比
        if isinstance(humidity, (int, float)):
            humidity = round(humidity * 100)
        
        # 转换气压为 hPa
        if isinstance(pressure, (int, float)):
            pressure = round(pressure / 100, 1)
        
        # 风速风向
        wind = realtime.get("wind", {})
        wind_speed = wind.get("speed", "--")
        wind_dir = wind.get("direction", "--")
        if isinstance(wind_dir, (int, float)):
            wind_dir = self._degree_to_direction(wind_dir)
        
        # 天气描述
        description = CAIYUN_SKYCON_MAP.get(skycon, skycon)
        
        # 空气质量
        aqi_info = ""
        air_quality = realtime.get("air_quality", {})
        if air_quality:
            pm25 = air_quality.get("pm25", "--")
            aqi = air_quality.get("aqi", {}).get("chn", "--")
            if pm25 != "--" or aqi != "--":
                aqi_info = f"\n🌫 空气质量: AQI {aqi}, PM2.5 {pm25} μg/m³"
        
        # 生活指数
        life_info = ""
        life_index = realtime.get("life_index", {})
        if life_index:
            uv = life_index.get("ultraviolet", {})
            comfort = life_index.get("comfort", {})
            if uv or comfort:
                life_info = "\n📊 生活指数:"
                if uv:
                    life_info += f" 紫外线 {uv.get('desc', '--')}"
                if comfort:
                    life_info += f" 舒适度 {comfort.get('desc', '--')}"
        
        return (
            f"🌍 地点: {city}\n"
            f"🌤 天气: {description}\n"
            f"🌡 温度: {temperature}°C (体感 {apparent_temp}°C)\n"
            f"💧 湿度: {humidity}%\n"
            f"💨 风速: {wind_speed} m/s {wind_dir}\n"
            f"👁 能见度: {visibility} km\n"
            f"🔽 气压: {pressure} hPa"
            f"{aqi_info}"
            f"{life_info}"
        )
    
    def _format_caiyun_forecast(self, data: dict) -> str:
        """格式化彩云天气预报数据"""
        daily = data.get("daily", {})
        city = data.get("city", "未知")
        days = data.get("days", 3)
        
        temperatures = daily.get("temperature", [])
        skycons = daily.get("skycon", [])
        precipitations = daily.get("precipitation", [])
        winds = daily.get("wind", [])
        humidities = daily.get("humidity", [])
        astros = daily.get("astro", [])
        
        lines = [f"📍 {city} 天气预报"]
        lines.append("=" * 24)
        
        for i in range(min(days, len(temperatures))):
            # 日期
            date_str = temperatures[i].get("date", "") if i < len(temperatures) else ""
            day_label = "今天" if i == 0 else "明天" if i == 1 else "后天" if i == 2 else f"第{i+1}天"

            try:
                # 彩云天气 v2.6 返回 ISO 格式日期如 2026-04-29T00:00+08:00
                dt = datetime.fromisoformat(date_str)
                weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]
                date_str = f"{dt.month}/{dt.day} {weekday}"
            except Exception:
                pass

            # 温度
            temp = temperatures[i] if i < len(temperatures) else {}
            max_temp = temp.get("max", "--")
            min_temp = temp.get("min", "--")
            avg_temp = temp.get("avg", "--")
            
            # 天气现象
            skycon = skycons[i].get("value", "UNKNOWN") if i < len(skycons) else "UNKNOWN"
            description = CAIYUN_SKYCON_MAP.get(skycon, skycon)
            
            # 降水
            rain_info = ""
            if i < len(precipitations):
                precip = precipitations[i]
                prob = precip.get("probability", 0)
                if prob and prob > 0:
                    # 兼容 API 返回小数(0.7)或整数(70)两种格式
                    if isinstance(prob, (int, float)) and prob <= 1:
                        prob = round(prob * 100)
                    else:
                        prob = round(prob)
                    rain_info = f" 🌧 降雨概率: {prob}%"
            
            # 风
            wind_info = ""
            if i < len(winds):
                wind = winds[i]
                avg_wind = wind.get("avg", {})
                if avg_wind:
                    speed = avg_wind.get("speed", "--")
                    direction = avg_wind.get("direction", "--")
                    if isinstance(direction, (int, float)):
                        direction = self._degree_to_direction(direction)
                    wind_info = f" 💨 {speed} m/s {direction}"
            
            # 湿度
            humidity_info = ""
            if i < len(humidities):
                hum = humidities[i]
                avg_hum = hum.get("avg", 0)
                if avg_hum:
                    humidity_info = f" 💧 湿度: {round(avg_hum * 100)}%"
            
            # 日出日落
            astro_info = ""
            if i < len(astros):
                astro = astros[i]
                sunrise = astro.get("sunrise", {}).get("time", "")
                sunset = astro.get("sunset", {}).get("time", "")
                if sunrise and sunset:
                    astro_info = f"\n🌅 日出: {sunrise}  🌇 日落: {sunset}"
            
            lines.append(
                f"\n📅 {day_label} ({date_str})\n"
                f"🌤 天气: {description}{rain_info}\n"
                f"🌡 温度: {min_temp}°C ~ {max_temp}°C (平均 {avg_temp}°C)\n"
                f"{humidity_info}{wind_info}"
                f"{astro_info}"
            )
        
        return "\n".join(lines)
    
    def _degree_to_direction(self, degree: float) -> str:
        """将角度转换为风向"""
        directions = ["北", "北东北", "东北", "东东北", "东", "东东南", "东南", "南东南",
                      "南", "南西南", "西南", "西西南", "西", "西西北", "西北", "北西北"]
        index = round(degree / 22.5) % 16
        return directions[index]
    
    # ==================== wttr.in API ====================
    
    async def _fetch_wttr_raw(self, city: str) -> Optional[dict]:
        """Fetch raw weather data from wttr.in API"""
        url = f"{self.wttr_base_url}/{city}"
        params = {"format": "j1"}
        text = None
        
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            headers = {
                "User-Agent": "curl/7.68.0",
                "Accept": "application/json"
            }
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.warning(f"Weather API returned status {resp.status} for city: {city}")
                        return None
                    
                    text = await resp.text()
                    data = json.loads(text)
                    return data
                    
        except aiohttp.ClientError as e:
            logger.error(f"Network error fetching weather for {city}: {e}")
            return None
        except TimeoutError:
            logger.error(f"Timeout fetching weather for {city}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error for {city}: {e}")
            logger.debug(f"Response text: {text[:200] if text else 'N/A'}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching weather for {city}: {e}")
            logger.debug(traceback.format_exc())
            return None
    
    async def _fetch_wttr_weather(self, city: str) -> Optional[dict]:
        """Fetch and parse current weather data from wttr.in"""
        data = await self._fetch_wttr_raw(city)
        if data is None:
            return None
        return self._parse_wttr_weather(data, city)
    
    async def _fetch_wttr_forecast(self, city: str, days: int) -> Optional[dict]:
        """Fetch and parse forecast weather data from wttr.in"""
        data = await self._fetch_wttr_raw(city)
        if data is None:
            return None
        return self._parse_wttr_forecast(data, city, days)
    
    def _parse_wttr_weather(self, data: dict, city: str) -> Optional[dict]:
        """Parse and normalize current weather data from wttr.in"""
        try:
            current = data.get("current_condition", [])
            if not current:
                return None
            
            cur = current[0]
            nearest_area = data.get("nearest_area", [{}])[0]
            
            area_name = nearest_area.get("areaName", [{}])[0].get("value", city)
            country = nearest_area.get("country", [{}])[0].get("value", "")
            location = f"{area_name}, {country}" if country else area_name
            
            return {
                "city": location,
                "description": cur.get("weatherDesc", [{}])[0].get("value", "未知"),
                "temp_c": cur.get("temp_C", "--"),
                "feels_like_c": cur.get("FeelsLikeC", "--"),
                "humidity": cur.get("humidity", "--"),
                "wind_speed": cur.get("windspeedKmph", cur.get("windspeedKm/h", "--")),
                "wind_dir": cur.get("winddir16Point", ""),
                "visibility": cur.get("visibility", "--"),
                "pressure": cur.get("pressure", "--"),
                "observation_time": cur.get("observation_time", ""),
            }
        except Exception as e:
            logger.error(f"Error parsing weather data for {city}: {e}")
            return None
    
    def _parse_wttr_forecast(self, data: dict, city: str, days: int) -> Optional[dict]:
        """Parse and normalize forecast data from wttr.in"""
        try:
            nearest_area = data.get("nearest_area", [{}])[0]
            area_name = nearest_area.get("areaName", [{}])[0].get("value", city)
            country = nearest_area.get("country", [{}])[0].get("value", "")
            location = f"{area_name}, {country}" if country else area_name
            
            weather_list = data.get("weather", [])
            if not weather_list:
                return None
            
            forecast_days = []
            for i in range(min(days, len(weather_list))):
                day_data = weather_list[i]
                hourly = day_data.get("hourly", [])
                
                representative = None
                for h in hourly:
                    time_str = h.get("time", "")
                    if time_str in ("1200", "1500", "900"):
                        representative = h
                        break
                
                if representative is None and hourly:
                    representative = hourly[len(hourly) // 2]
                
                astronomy = day_data.get("astronomy", [{}])[0]
                
                day_info = {
                    "date": day_data.get("date", ""),
                    "maxtemp_c": day_data.get("maxtempC", "--"),
                    "mintemp_c": day_data.get("mintempC", "--"),
                    "avgtemp_c": day_data.get("avgtempC", "--"),
                    "description": "",
                    "chance_of_rain": "0",
                    "wind_speed": "--",
                    "wind_dir": "",
                    "humidity": "--",
                    "uv_index": day_data.get("uvIndex", "--"),
                    "sunrise": astronomy.get("sunrise", ""),
                    "sunset": astronomy.get("sunset", ""),
                }
                
                if representative:
                    desc = representative.get("weatherDesc", [{}])[0].get("value", "未知")
                    day_info["description"] = desc
                    day_info["chance_of_rain"] = representative.get("chanceofrain", "0")
                    day_info["wind_speed"] = representative.get("windspeedKmph", "--")
                    day_info["wind_dir"] = representative.get("winddir16Point", "")
                    day_info["humidity"] = representative.get("humidity", "--")
                
                forecast_days.append(day_info)
            
            return {
                "city": location,
                "days": len(forecast_days),
                "forecast": forecast_days,
            }
        except Exception as e:
            logger.error(f"Error parsing forecast data for {city}: {e}")
            return None
    
    def _format_wttr_weather(self, data: dict) -> str:
        """Format current weather data from wttr.in"""
        wind_info = f"{data['wind_speed']} km/h"
        if data.get('wind_dir'):
            wind_info += f" {data['wind_dir']}"
        
        return (
            f"🌍 地点: {data['city']}\n"
            f"🌤 天气: {data['description']}\n"
            f"🌡 温度: {data['temp_c']}°C (体感 {data['feels_like_c']}°C)\n"
            f"💧 湿度: {data['humidity']}%\n"
            f"💨 风速: {wind_info}\n"
            f"👁 能见度: {data['visibility']} km\n"
            f"🔽 气压: {data['pressure']} hPa\n"
            f"⏰ 观测时间: {data['observation_time']}"
        )
    
    def _format_wttr_forecast(self, data: dict) -> str:
        """Format forecast data from wttr.in"""
        lines = [f"📍 {data['city']} 天气预报"]
        lines.append("=" * 24)
        
        for i, day in enumerate(data['forecast']):
            if i == 0:
                day_label = "今天"
            elif i == 1:
                day_label = "明天"
            elif i == 2:
                day_label = "后天"
            else:
                day_label = f"第{i+1}天"
            
            date_str = day['date']
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]
                date_str = f"{dt.month}/{dt.day} {weekday}"
            except Exception:
                pass
            
            desc = day['description'].strip()
            rain_info = ""
            if day.get('chance_of_rain') and str(day['chance_of_rain']) != "0":
                rain_info = f" 🌧 降雨概率: {day['chance_of_rain']}%"
            
            wind_info = ""
            if day.get('wind_speed') and day['wind_speed'] != "--":
                wind_info = f" 💨 {day['wind_speed']} km/h"
                if day.get('wind_dir'):
                    wind_info += f" {day['wind_dir']}"
            
            humidity_info = ""
            if day.get('humidity') and day['humidity'] != "--":
                humidity_info = f" 💧 湿度: {day['humidity']}%"
            
            lines.append(
                f"\n📅 {day_label} ({date_str})\n"
                f"🌤 天气: {desc}{rain_info}\n"
                f"🌡 温度: {day['mintemp_c']}°C ~ {day['maxtemp_c']}°C (平均 {day['avgtemp_c']}°C)\n"
                f"{humidity_info}{wind_info}"
            )
            
            if day.get('sunrise') and day.get('sunset'):
                lines.append(f"🌅 日出: {day['sunrise']}  🌇 日落: {day['sunset']}")
        
        return "\n".join(lines)
    
    # ==================== 工具方法 ====================
    
    def _parse_days(self, text: str) -> int:
        """Parse days parameter from text input"""
        text = text.strip().lower()
        
        if text in ("明天", "明日", "tomorrow"):
            return 1
        if text in ("后天", "後天", "day after tomorrow"):
            return 2
        if text in ("大后天", "大後天", "三天后", "3天后"):
            return 3
        
        try:
            days = int(text)
            if days < 0:
                return 0
            if days > 7:
                return 7  # 限制最多7天
            return days
        except ValueError:
            return 0
    
    async def terminate(self):
        """Cleanup hook - called when plugin unloads"""
        logger.info("天气插件已卸载")
