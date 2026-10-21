"""Ferramenta do clima (Fase 10): previsão do Open-Meteo, grátis e sem chave.

- clima(cidade): agora (temperatura, sensação, condição) e os próximos 3 dias (mínima, máxima,
  chance de chuva, condição).

A cidade vem da pergunta ou da memória (fato "A cidade do Reinaldo para o clima é Novo Hamburgo
(RS)"): o LLM preenche. O estado entre parênteses, depois de vírgula ou hífen ("Novo Hamburgo
(RS)", "Porto Alegre, RS") separa as homônimas; sem estado, vale a mais populosa do Brasil.

Sai para a internet só o nome da cidade e as coordenadas (o Reinaldo autorizou em 21/10).
"""

import asyncio
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date

import httpx

from .base import Tool

log = logging.getLogger(__name__)

GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
FORECAST_TTL_S = 600  # "e amanhã?" logo depois não consulta de novo
DAYS = 3

UF = {"AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia", "CE": "Ceará",
      "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás", "MA": "Maranhão", "MT": "Mato Grosso",
      "MS": "Mato Grosso do Sul", "MG": "Minas Gerais", "PA": "Pará", "PB": "Paraíba", "PR": "Paraná",
      "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte",
      "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima", "SC": "Santa Catarina", "SP": "São Paulo",
      "SE": "Sergipe", "TO": "Tocantins"}

# Códigos WMO do Open-Meteo, em palavras para a fala
CONDITIONS = {0: "céu limpo", 1: "quase sem nuvens", 2: "parcialmente nublado", 3: "nublado", 45: "neblina",
              48: "neblina", 51: "garoa fraca", 53: "garoa", 55: "garoa forte", 56: "garoa gelada",
              57: "garoa gelada", 61: "chuva fraca", 63: "chuva", 65: "chuva forte", 66: "chuva gelada",
              67: "chuva gelada", 71: "neve fraca", 73: "neve", 75: "neve forte", 77: "neve", 80: "pancadas de chuva fracas",
              81: "pancadas de chuva", 82: "pancadas de chuva fortes", 85: "pancadas de neve", 86: "pancadas de neve",
              95: "trovoada", 96: "trovoada com granizo", 99: "trovoada com granizo"}
WEEKDAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


@dataclass(frozen=True)
class WeatherConfig:
    enabled: bool = True


@dataclass(frozen=True)
class Place:
    name: str
    state: str
    latitude: float
    longitude: float
    timezone: str


class WeatherError(Exception):
    pass


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c)).strip()


def split_city(text: str) -> tuple[str, str | None]:
    """("Novo Hamburgo", "Rio Grande do Sul") a partir de "Novo Hamburgo (RS)", "Novo Hamburgo, RS",
    "Novo Hamburgo - Rio Grande do Sul"... O estado é None se não vier."""
    text = " ".join(text.split())
    m = re.match(r"^(.*?)\s*(?:\((.+?)\)|[,/-]\s*(.+))\s*$", text)
    if not m:
        return text, None
    city, state = m.group(1), (m.group(2) or m.group(3)).strip()
    if state.upper() in UF:
        return city, UF[state.upper()]
    for name in UF.values():
        if _plain(state) == _plain(name):
            return city, name
    return text, None  # "Rio de Janeiro - Centro": não é estado, fica tudo como nome


def choose(results: list[dict], city: str, state: str | None) -> dict | None:
    """Cidade (não aeroporto ou bairro) com o nome pedido, do estado pedido; entre homônimas, a do
    Brasil e mais populosa."""
    cities = [r for r in results if str(r.get("feature_code", "")).startswith("PPL")]
    if state:
        cities = [r for r in cities if _plain(r.get("admin1", "")) == _plain(state)]
    exact = [r for r in cities if _plain(r.get("name", "")) == _plain(city)]
    pool = exact or cities
    if not pool:
        return None
    return max(pool, key=lambda r: (r.get("country_code") == "BR", r.get("population") or 0))


class WeatherClient:
    """Cliente HTTP síncrono, numa thread, com uma conexão por servidor reaproveitada e até 3
    tentativas (o mesmo cuidado do GitHub, ADR-020: conexões novas às vezes não abrem)."""

    def __init__(self, config: WeatherConfig = WeatherConfig(), transport: httpx.BaseTransport | None = None,
                 clock=time.monotonic):
        self.config = config
        self._client = httpx.Client(timeout=httpx.Timeout(5.0, connect=2.5), transport=transport,
                                    headers={"User-Agent": "agatha-srv"})
        self._clock = clock
        self._places: dict[str, Place | None] = {}
        self._forecasts: dict[tuple[float, float], tuple[float, dict]] = {}

    def _get(self, url: str, params: dict) -> dict:
        for attempt in (1, 2, 3):
            try:
                r = self._client.get(url, params=params)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                if attempt == 3:
                    raise WeatherError(f"sem conexão com o serviço do clima ({type(e).__name__})") from e
                time.sleep(0.3)
            except httpx.HTTPError as e:
                raise WeatherError(f"sem conexão com o serviço do clima ({type(e).__name__})") from e
        if r.status_code >= 400:
            raise WeatherError(f"o serviço do clima respondeu {r.status_code}")
        return r.json()

    def place(self, text: str) -> Place | None:
        city, state = split_city(text)
        key = f"{_plain(city)}|{state or ''}"
        if key not in self._places:
            data = self._get(GEOCODING, {"name": city, "count": 10, "language": "pt"})
            r = choose(data.get("results") or [], city, state)
            self._places[key] = r and Place(r["name"], r.get("admin1", ""), r["latitude"], r["longitude"],
                                            r.get("timezone") or "America/Sao_Paulo")
        return self._places[key]

    def forecast(self, place: Place) -> dict:
        key = (place.latitude, place.longitude)
        cached = self._forecasts.get(key)
        if cached and self._clock() - cached[0] < FORECAST_TTL_S:
            return cached[1]
        data = self._get(FORECAST, {
            "latitude": place.latitude, "longitude": place.longitude, "timezone": place.timezone,
            "current": "temperature_2m,apparent_temperature,weather_code",
            "daily": "temperature_2m_min,temperature_2m_max,precipitation_probability_max,weather_code",
            "forecast_days": DAYS,
        })
        self._forecasts[key] = (self._clock(), data)
        return data

    async def weather(self, text: str) -> dict:
        return await asyncio.to_thread(self._weather, text)

    def _weather(self, text: str) -> dict:
        place = self.place(text)
        if place is None:
            return {"erro": f"não achei a cidade {text!r}"}
        return summarize(place, self.forecast(place))

    async def aclose(self) -> None:
        self._client.close()


def _day_name(day: date, today: date) -> str:
    offset = (day - today).days
    return {0: "hoje", 1: "amanhã"}.get(offset, WEEKDAYS[day.weekday()])


def summarize(place: Place, data: dict) -> dict:
    """Só o que vira fala, em poucos tokens: temperaturas inteiras em graus Celsius."""
    current, daily = data["current"], data["daily"]
    today = date.fromisoformat(daily["time"][0])
    state = next((uf for uf, name in UF.items() if name == place.state), place.state)
    days = []
    for i, day in enumerate(daily["time"]):
        rain = daily["precipitation_probability_max"][i]
        days.append({"dia": _day_name(date.fromisoformat(day), today),
                     "minima": round(daily["temperature_2m_min"][i]), "maxima": round(daily["temperature_2m_max"][i]),
                     "chance_de_chuva": f"{rain}%" if rain is not None else "sem dado",
                     "condicao": CONDITIONS.get(daily["weather_code"][i], "sem dado")})
    return {
        "cidade": f"{place.name} ({state})" if state else place.name,
        "agora": {"temperatura": round(current["temperature_2m"]), "sensacao": round(current["apparent_temperature"]),
                  "condicao": CONDITIONS.get(current["weather_code"], "sem dado")},
        "previsao": days,
        "unidade": "graus Celsius",
    }


def weather_tools(client: WeatherClient) -> list[Tool]:
    async def clima(args: dict) -> dict:
        city = (args.get("cidade") or "").strip()
        if not city:
            return {"erro": "faltou a cidade: pergunte a ele qual é"}
        try:
            return await client.weather(city)
        except WeatherError as e:
            log.warning("clima falhou: %s", e)
            return {"erro": str(e)}

    return [Tool(
        name="clima",
        description=f"Mostra o tempo agora e a previsão de {DAYS} dias de uma cidade: temperatura, chance de chuva e "
                    "condição.",
        handler=clima,
        rule="perguntas sobre o tempo, temperatura, chuva ou previsão: use clima com a cidade que ele disser ou a da "
             "sua memória; se não souber a cidade, pergunte",
        properties={"cidade": {"type": "string", "description": "cidade e estado, como Curitiba (PR)"}},
        required=("cidade",),
    )]
