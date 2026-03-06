# FVE Solar Forecast – Home Assistant Add-on

Home Assistant Add-on pro predikci výroby solární elektrárny s automatickým self-learning kalibrací.

## Co to dělá

- Každou hodinu stahuje hodinovou předpověď výroby z **[forecast.solar](https://forecast.solar)** (zdarma, bez registrace)
- Každých 5 minut loguje skutečný výkon z vašeho HA senzoru (Solar Assistant / libovolný střídač)
- Každý večer porovná předpověď se skutečností a **postupně zpřesňuje** korekční faktory
- Stahuje počasí z **Open-Meteo** (oblačnost, teplota) pro analýzu chyb
- Zobrazuje vše v přehledném **webovém dashboardu**

## Screenshoty

Dashboard obsahuje 4 stránky:

| **Dnes** | **Týden** | **Přesnost** | **⚡ Toky** |
|---|---|---|---|
| Sloupcový graf forecast vs. skutečnost | 7d zpět + 7d dopředu | 7 grafů: MAPE, korekční faktor, hodinová chyba, oblačnost, histogram | Živé schéma toků energie (FV → střídač → dům / baterie / síť) |

## Instalace do Home Assistant

### 1. Přidejte repozitář

V HA: **Nastavení → Doplňky → Add-on Store → ⋮ (tři tečky) → Repositories**

Přidejte URL tohoto repozitáře.

### 2. Nainstalujte doplněk

Najděte „FVE Solar Forecast" v seznamu a klikněte **Install**.

### 3. Konfigurace

Před spuštěním vyplňte v záložce **Configuration**:

```yaml
latitude: 50.07        # GPS šířka vaší instalace
longitude: 14.44       # GPS délka
tilt: 30               # Sklon panelů ve stupních (0=vodorovně, 90=svisle)
azimuth: 180           # Azimut panelů (0=S, 90=V, 180=J, 270=Z)
kwp: 5.0               # Instalovaný výkon v kWp
ha_sensor_power: "sensor.solar_assistant_pv_power"         # Senzor okamžitého výkonu (W)
ha_sensor_energy: "sensor.solar_assistant_pv_energy_today" # Senzor denní energie (kWh)

# Volitelné – pro dashboard Toky:
ha_sensor_battery_soc: "sensor.battery_state_of_charge"    # SOC baterie (%)
ha_sensor_battery_power: "sensor.battery_power"            # Výkon baterie (W, + = nabíjení)
ha_sensor_battery_voltage: "sensor.battery_voltage"        # Napětí baterie (V)
ha_sensor_grid_power: "sensor.grid_power"                  # Příkon ze sítě (W, pouze import)
ha_sensor_load_power: "sensor.load_power"                  # Spotřeba domu (W)
ha_sensor_inverter_mode: "sensor.axpert_king_35_device_mode"  # Pracovní stav střídače
timezone: "Europe/Prague"                                      # Lokální časové pásmo (výchozí)
```

Token (`ha_token`) **není potřeba** – add-on používá automatický `SUPERVISOR_TOKEN`.

> **Jak zjistit přesné názvy senzorů?**
> V HA: Developer Tools → States, vyhledejte „solar_assistant" nebo název svého střídače.

### 4. Spusťte

Klikněte **Start** a pak **Open Web UI** (nebo ikona v sidebaru).

## Fáze kalibrace

| Fáze | Podmínka | Co se děje |
|---|---|---|
| **warmup** | 0 dní dat | Předpověď bez korekce (faktor 1,0) |
| **phase1** | 1+ dní | Globální korekční faktor (rolling 14d průměr actual/forecast) |
| **phase2** | 14+ dní | + Korekce dle části dne (ráno / poledne / odpoledne) |

## Metriky přesnosti

- **RMSE** – celková chyba (penalizuje velké odchylky)
- **MAE** – průměrná absolutní chyba
- **MBE** – systematická chyba (bias): + = nadodhad, − = pododhad
- **MAPE** – relativní chyba v %
- **Skill score** – o kolik % je kalibrovaná předpověď lepší než raw

## API

Aplikace vystavuje REST API (přístupné i z jiných systémů):

| Endpoint | Popis |
|---|---|
| `GET /api/forecast/today` | Hodinová předpověď na dnes (raw + kalibrovaná + skutečnost) |
| `GET /api/forecast/week` | 7d zpět + 7d dopředu |
| `GET /api/accuracy` | Denní metriky + souhrnné statistiky |
| `GET /api/accuracy/hourly` | RMSE/MAE/MBE pro každou hodinu dne |
| `GET /api/accuracy/weather` | Přesnost dle oblačnosti |
| `GET /api/flow` | Aktuální toky energie (cache, obnovuje se každé 2 s) |
| `GET /api/status` | Health check |

## Technologie

- Python 3.12 · FastAPI · APScheduler · aiosqlite · httpx · Chart.js
- Datové zdroje: [forecast.solar](https://forecast.solar) · [Open-Meteo](https://open-meteo.com)
- Běží jako HA Add-on s Ingress (přístup přes HA sidebar, bez otevírání portů)

## Střídače / monitoring

Funguje s jakýmkoliv střídačem, který vystavuje HA senzory výkonu (W) a energie (kWh):
- Solar Assistant (Axpert, Voltronic, ...)
- SolarEdge, Fronius, SolaX, Enphase, Goodwe, ...
