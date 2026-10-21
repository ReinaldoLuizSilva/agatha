"""Ferramenta do clima com o Open-Meteo simulado (sem rede)."""

import asyncio

import httpx
import pytest

from agatha_server.tools.weather import WeatherClient, choose, split_city, weather_tools

GEO = {"results": [
    {"name": "Novo Hamburgo", "feature_code": "PPL", "country_code": "BR", "admin1": "Rio Grande do Sul",
     "latitude": -29.68, "longitude": -51.13, "timezone": "America/Sao_Paulo", "population": 253841},
    {"name": "Novo Hamburgo Airport", "feature_code": "AIRP", "country_code": "BR", "admin1": "Rio Grande do Sul",
     "latitude": -29.70, "longitude": -51.08, "timezone": "America/Sao_Paulo"},
]}
PORTO_ALEGRE = [
    {"name": "Porto Alegre", "feature_code": "PPLA", "country_code": "BR", "admin1": "Rio Grande do Sul",
     "latitude": -30.03, "longitude": -51.23, "population": 1488252},
    {"name": "Porto Alegre do Piauí", "feature_code": "PPL", "country_code": "BR", "admin1": "Piauí",
     "latitude": -6.96, "longitude": -44.19, "population": 2364},
    {"name": "Porto Alegre", "feature_code": "PPL", "country_code": "ST", "admin1": "São Tomé",
     "latitude": 0.03, "longitude": 6.53, "population": 795},
]
FORECAST = {
    "current": {"temperature_2m": 18.4, "apparent_temperature": 16.6, "weather_code": 3},
    "daily": {"time": ["2026-10-21", "2026-10-22", "2026-10-23"], "temperature_2m_min": [13.6, 15.1, 12.0],
              "temperature_2m_max": [22.4, 19.0, 24.5], "precipitation_probability_max": [10, 85, None],
              "weather_code": [2, 63, 0]},
}


def tool(api, clock=lambda: 0.0):
    client = WeatherClient(transport=httpx.MockTransport(api), clock=clock)
    (clima,) = weather_tools(client)
    return clima


def fake_api(calls):
    def api(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host.startswith("geocoding"):
            assert request.url.params["name"] == "Novo Hamburgo"
            return httpx.Response(200, json=GEO)
        assert request.url.params["latitude"] == "-29.68"
        return httpx.Response(200, json=FORECAST)
    return api


def test_agora_e_proximos_dias():
    calls = []
    result = asyncio.run(tool(fake_api(calls)).handler({"cidade": "Novo Hamburgo (RS)"}))
    assert result == {
        "cidade": "Novo Hamburgo",
        "agora": "18 graus, nublado",  # sensação 17: diferença pequena, fica de fora
        "hoje": "de 14 a 22 graus, chance de chuva 10%, parcialmente nublado",
        "amanhã": "de 15 a 19 graus, chance de chuva 85%, chuva",
        "sexta-feira": "de 12 a 24 graus, chance de chuva sem dado, céu limpo",
    }


def test_sensacao_aparece_quando_difere():
    data = {**FORECAST, "current": {"temperature_2m": 30.2, "apparent_temperature": 34.6, "weather_code": 0}}
    calls = []

    def api(request):
        calls.append(1)
        return httpx.Response(200, json=GEO if request.url.host.startswith("geocoding") else data)

    assert asyncio.run(tool(api).handler({"cidade": "Novo Hamburgo"}))["agora"] == "30 graus, sensação de 35, céu limpo"


def test_cidade_e_previsao_ficam_em_cache():
    calls, now = [], [0.0]
    clima = tool(fake_api(calls), clock=lambda: now[0])
    asyncio.run(clima.handler({"cidade": "Novo Hamburgo (RS)"}))
    asyncio.run(clima.handler({"cidade": "novo hamburgo, RS"}))  # "e amanhã?" logo depois
    assert len(calls) == 2
    now[0] = 601  # passou o prazo da previsão; a cidade continua no cache
    asyncio.run(clima.handler({"cidade": "Novo Hamburgo (RS)"}))
    assert len(calls) == 3 and calls[-1].startswith("api.")


def test_sem_cidade_pede_para_perguntar():
    assert "pergunte" in asyncio.run(tool(fake_api([])).handler({}))["erro"]


def test_cidade_desconhecida():
    clima = tool(lambda request: httpx.Response(200, json={"generationtime_ms": 0.1}))
    assert "não achei" in asyncio.run(clima.handler({"cidade": "Xpto"}))["erro"]


def test_falha_de_conexao_vira_erro_depois_de_3_tentativas(monkeypatch):
    monkeypatch.setattr("agatha_server.tools.weather.time.sleep", lambda s: None)
    calls = []

    def api(request):
        calls.append(1)
        raise httpx.ConnectError("sem rede", request=request)

    assert "sem conexão" in asyncio.run(tool(api).handler({"cidade": "Novo Hamburgo"}))["erro"]
    assert len(calls) == 3


@pytest.mark.parametrize("text, expected", [
    ("Novo Hamburgo (RS)", ("Novo Hamburgo", "Rio Grande do Sul")),
    ("Porto Alegre, rs", ("Porto Alegre", "Rio Grande do Sul")),
    ("Curitiba - Paraná", ("Curitiba", "Paraná")),
    ("São Paulo", ("São Paulo", None)),
    ("Rio de Janeiro - Centro", ("Rio de Janeiro - Centro", None)),
])
def test_separa_cidade_e_estado(text, expected):
    assert split_city(text) == expected


def test_escolhe_a_cidade_do_estado_e_a_mais_populosa():
    assert choose(PORTO_ALEGRE, "Porto Alegre", None)["admin1"] == "Rio Grande do Sul"
    # sem o nome exato no estado, vale a parecida de lá: "Porto Alegre, PI" é Porto Alegre do Piauí
    assert choose(PORTO_ALEGRE, "Porto Alegre", "Piauí")["name"] == "Porto Alegre do Piauí"
    assert choose(PORTO_ALEGRE, "Porto Alegre", "Bahia") is None
