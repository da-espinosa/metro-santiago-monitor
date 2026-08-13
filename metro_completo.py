"""
Script v3 (completo): consulta, parsea y GUARDA en Supabase el estado
de la Red de Metro de Santiago, usando la API comunitaria de xorcl
(https://github.com/xorcl/api-red)

Estructura confirmada de la respuesta de la API:
  Response
    ├── api_status
    ├── time (viene en UTC)
    ├── issues (bool)          -> True si hay algún problema en la red
    └── lines []                -> SOLO contiene líneas con problemas (vacío = todo OK)
          ├── name, id
          ├── issues (bool)
          ├── stations_closed_by_schedule (int)
          └── stations []
                ├── name, id
                ├── status (código: 0=operativa, 1=cerrada temporal,
                │           2=no habilitada, 3=accesos cerrados)
                ├── lines []
                ├── description
                ├── reason
                └── is_closed_by_schedule

"""

import os
import sys
import requests
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from supabase import create_client, Client

# ============================================
# CONFIGURACIÓN — se lee desde variables de entorno,
# nunca escrita directamente acá (así el código es seguro
# para subir a un repo público de GitHub)
# ============================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("Error: faltan las variables de entorno SUPABASE_URL y/o SUPABASE_KEY.")
    print("Configuralas antes de correr el script (ver README).")
    sys.exit(1)

METRO_API_URL = "https://api.xor.cl/red/metro-network"
TZ_CHILE = ZoneInfo("America/Santiago")


def parse_api_time_to_chile(time_str: str) -> str:
    """
    El campo 'time' de la API viene en UTC (confirmado empíricamente:
    04:16 UTC == 00:16 hora Chile en invierno). Lo convertimos a hora
    local de Chile, respetando cambios de horario de verano automáticamente
    gracias a zoneinfo.
    """
    try:
        dt_utc = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=ZoneInfo("UTC")
        )
        dt_chile = dt_utc.astimezone(TZ_CHILE)
        return dt_chile.strftime("%Y-%m-%d %H:%M:%S (%Z)")
    except (ValueError, TypeError):
        return time_str  # si algo falla, devolvemos el original sin romper el script


def fetch_metro_status() -> dict:
    """Consulta la API y devuelve el JSON crudo. Lanza excepción si falla."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    response = requests.get(METRO_API_URL, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def parse_status(data: dict) -> dict:
    """
    Convierte la respuesta cruda en una estructura simplificada,
    lista para imprimir y para guardar en base de datos.
    """
    lineas_con_problemas = []

    for linea in data.get("lines", []):
        estaciones_afectadas = []
        for estacion in linea.get("stations", []):
            estaciones_afectadas.append({
                "nombre": estacion.get("name"),
                "id": estacion.get("id"),
                "status_code": estacion.get("status"),
                "descripcion": estacion.get("description", ""),
                "razon": estacion.get("reason", ""),
                "cerrada_por_horario": estacion.get("is_closed_by_schedule", False),
            })

        lineas_con_problemas.append({
            "linea_nombre": linea.get("name"),
            "linea_id": linea.get("id"),
            "estaciones_cerradas_por_horario": linea.get("stations_closed_by_schedule", 0),
            "estaciones_afectadas": estaciones_afectadas,
        })

    return {
        "timestamp_consulta": datetime.now(TZ_CHILE).isoformat(),
        "timestamp_api_utc": data.get("time"),
        "timestamp_api_chile": parse_api_time_to_chile(data.get("time")),
        "red_ok": not data.get("issues", False),
        "lineas_con_problemas": lineas_con_problemas,
    }


def print_summary(parsed: dict):
    """Imprime un resumen legible en consola."""
    print(f"\n Consultado:       {parsed['timestamp_consulta']}")
    print(f"Hora API (UTC):   {parsed['timestamp_api_utc']}")
    print(f"Hora API (Chile): {parsed['timestamp_api_chile']}")

    if parsed["red_ok"]:
        print("\n Toda la Red de Metro está funcionando con normalidad.")
        return

    print(f"\n  Se detectaron problemas en {len(parsed['lineas_con_problemas'])} línea(s):\n")
    for linea in parsed["lineas_con_problemas"]:
        print(f"   {linea['linea_nombre']} (id: {linea['linea_id']})")
        for est in linea["estaciones_afectadas"]:
            print(f"     - {est['nombre']}: {est['descripcion'] or est['razon'] or 'sin detalle'}")
        print()


def guardar_en_supabase(supabase: Client, data: dict) -> int:
    """
    Guarda el snapshot y, si corresponde, los incidentes en Supabase.
    Devuelve el id del snapshot creado.
    """
    red_ok = not data.get("issues", False)

    # 1. Insertamos el snapshot (siempre, haya o no problemas)
    snapshot_result = supabase.table("snapshots_estado").insert({
        "red_ok": red_ok
        # timestamp_chile se llena solo con el default now() de la tabla
    }).execute()

    snapshot_id = snapshot_result.data[0]["id"]
    print(f"Snapshot #{snapshot_id} guardado en Supabase (red_ok={red_ok})")

    # 2. Si hay incidentes, los insertamos relacionados a ese snapshot
    incidentes_a_insertar = []
    for linea in data.get("lines", []):
        linea_id = linea.get("id")
        for estacion in linea.get("stations", []):
            incidentes_a_insertar.append({
                "snapshot_id": snapshot_id,
                "linea_id": linea_id,
                "estacion_nombre": estacion.get("name"),
                "estacion_id": estacion.get("id"),
                "status_code": estacion.get("status"),
                "descripcion": estacion.get("description", ""),
                "razon": estacion.get("reason", ""),
                "cerrada_por_horario": estacion.get("is_closed_by_schedule", False),
            })

    if incidentes_a_insertar:
        supabase.table("incidentes_estacion").insert(incidentes_a_insertar).execute()
        print(f"  {len(incidentes_a_insertar)} incidente(s) guardado(s) en Supabase")
    else:
        print("Sin incidentes que registrar en Supabase")

    return snapshot_id


def main():
    print(f"Consultando {METRO_API_URL} ...")

    try:
        # 1. Traemos el dato crudo de la API
        data = fetch_metro_status()

        # 2. Lo parseamos a una estructura legible
        parsed = parse_status(data)
        print_summary(parsed)

        # 3. Lo guardamos también como archivo local, para inspección manual
        with open("metro_status_raw.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        with open("metro_status_parsed.json", "w", encoding="utf-8") as f:
            json.dump(parsed, f, indent=2, ensure_ascii=False)
        print("Guardado local: metro_status_raw.json y metro_status_parsed.json")

        # 4. Lo guardamos en Supabase
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        guardar_en_supabase(supabase, data)

        print("\n Proceso completado.")

    except requests.exceptions.Timeout:
        print("Error: la API del Metro no respondió a tiempo (timeout).")
    except requests.exceptions.HTTPError as e:
        print(f"Error HTTP consultando la API del Metro: {e}")
    except requests.exceptions.RequestException as e:
        print(f"Error de conexión con la API del Metro: {e}")
    except json.JSONDecodeError:
        print("La respuesta de la API no es un JSON válido.")
    except Exception as e:
        print(f"Error guardando en Supabase: {e}")


if __name__ == "__main__":
    main()