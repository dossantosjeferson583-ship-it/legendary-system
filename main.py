
import os
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

API_KEY = os.getenv("API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

API_URL = "https://v3.football.api-sports.io"
FUSO = ZoneInfo("America/Sao_Paulo")

HEADERS = {
    "x-apisports-key": API_KEY,
    "x-apisports-host": "v3.football.api-sports.io",
}

CONFIG = {
    "historico": int(os.getenv("HISTORICO_JOGOS", "10")),
    "min_jogos_historico": int(os.getenv("MIN_JOGOS_HISTORICO", "5")),
    "indice_ht_minimo": float(os.getenv("INDICE_HT_MINIMO", "70")),
    "top_alertas": int(os.getenv("TOP_ALERTAS", "5")),
    "max_jogos_analisar": int(os.getenv("MAX_JOGOS_ANALISAR", "20")),
    "intervalo_monitor": int(os.getenv("INTERVALO_MONITOR", "60")),
    "hora_scan": int(os.getenv("HORA_SCAN", "7")),
    "minuto_scan": int(os.getenv("MINUTO_SCAN", "0")),
    "janela_antes_inicio_min": int(os.getenv("JANELA_ANTES_INICIO_MIN", "10")),
    "janela_depois_inicio_min": int(os.getenv("JANELA_DEPOIS_INICIO_MIN", "150")),
}

CACHE_HISTORICO = {}
MARCOS_ENVIADOS = set()
SELECIONADOS_ATUAIS = []
ULTIMA_DATA_SCAN = None


def log(msg):
    agora = datetime.now(FUSO).strftime("%d/%m/%Y %H:%M:%S")
    print(f"[{agora}] {msg}", flush=True)


def validar_configuracao():
    faltando = []
    if not API_KEY:
        faltando.append("API_KEY")
    if not TELEGRAM_TOKEN:
        faltando.append("TELEGRAM_TOKEN")
    if not TELEGRAM_CHAT_ID:
        faltando.append("TELEGRAM_CHAT_ID")
    if faltando:
        raise RuntimeError(
            "Variáveis de ambiente ausentes: " + ", ".join(faltando)
        )


def api_get(endpoint, params=None):
    try:
        r = requests.get(
            f"{API_URL}/{endpoint.lstrip('/')}",
            headers=HEADERS,
            params=params or {},
            timeout=30,
        )
        dados = r.json()
        erros = dados.get("errors") or {}
        if erros:
            log(f"⚠️ API {endpoint}: {erros}")
            return None
        if r.status_code != 200:
            log(f"⚠️ HTTP {r.status_code} em {endpoint}")
            return None
        return dados.get("response", [])
    except Exception as erro:
        log(f"❌ Erro API {endpoint}: {erro}")
        return None


def telegram(texto):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": texto,
            },
            timeout=20,
        )
        dados = r.json()
        if r.status_code == 200 and dados.get("ok"):
            log("📨 Telegram: OK")
            return True
        log(f"⚠️ Telegram: {dados}")
        return False
    except Exception as erro:
        log(f"❌ Telegram: {erro}")
        return False


def testar_conexoes():
    api = api_get("status")
    api_ok = api is not None
    tg_ok = telegram("🤖 Monitor estatístico iniciado no servidor.")
    log(f"API: {api_ok} | Telegram: {tg_ok}")
    return api_ok and tg_ok


def buscar_jogos_do_dia():
    data = datetime.now(FUSO).strftime("%Y-%m-%d")
    jogos = api_get("fixtures", {"date": data})
    if jogos is None:
        return []

    validos = []
    for jogo in jogos:
        fixture = jogo.get("fixture", {})
        status = fixture.get("status", {}).get("short")
        liga = jogo.get("league", {}).get("name", "").lower()

        if status not in {"NS", "TBD", "1H", "HT", "2H", "LIVE"}:
            continue
        if "friendly" in liga:
            continue

        texto = (
            jogo.get("teams", {}).get("home", {}).get("name", "")
            + " "
            + jogo.get("teams", {}).get("away", {}).get("name", "")
            + " "
            + liga
        ).lower()

        if any(termo in texto for termo in ["u17", "u18", "u19", "u20", "u21", "u23"]):
            continue

        validos.append(jogo)

    log(f"📅 Jogos encontrados: {len(jogos)}")
    log(f"🔎 Jogos após filtro: {len(validos)}")
    return validos


def buscar_historico(team_id, season):
    chave = (team_id, season)
    if chave in CACHE_HISTORICO:
        return CACHE_HISTORICO[chave]

    jogos = api_get(
        "fixtures",
        {"team": team_id, "season": season, "status": "FT"},
    )
    if jogos is None:
        return []

    jogos = sorted(
        jogos,
        key=lambda x: x.get("fixture", {}).get("timestamp", 0),
        reverse=True,
    )[:CONFIG["historico"]]

    CACHE_HISTORICO[chave] = jogos
    return jogos


def metricas_ht(jogos):
    validos = []
    for jogo in jogos:
        ht = jogo.get("score", {}).get("halftime", {})
        casa = ht.get("home")
        fora = ht.get("away")
        if casa is None or fora is None:
            continue
        validos.append(int(casa) + int(fora))

    total = len(validos)
    if total == 0:
        return {"jogos": 0, "gol_ht": 0, "zero_zero": 100, "media": 0}

    gol_ht = sum(1 for gols in validos if gols >= 1)
    zero_zero = sum(1 for gols in validos if gols == 0)

    return {
        "jogos": total,
        "gol_ht": round(gol_ht / total * 100, 1),
        "zero_zero": round(zero_zero / total * 100, 1),
        "media": round(sum(validos) / total, 2),
    }


def calcular_indice_ht(casa, fora):
    if (
        casa["jogos"] < CONFIG["min_jogos_historico"]
        or fora["jogos"] < CONFIG["min_jogos_historico"]
    ):
        return 0

    freq = (casa["gol_ht"] + fora["gol_ht"]) / 2
    zero = (casa["zero_zero"] + fora["zero_zero"]) / 2
    media = (casa["media"] + fora["media"]) / 2
    media_score = min(media / 1.5 * 100, 100)

    indice = freq * 0.55 + (100 - zero) * 0.25 + media_score * 0.20
    return round(min(indice, 100), 1)


def horario_brasilia(data_api):
    try:
        data = datetime.fromisoformat(data_api.replace("Z", "+00:00"))
        return data.astimezone(FUSO).strftime("%d/%m/%Y às %H:%M")
    except Exception:
        return "Horário indisponível"


def analisar_jogo(jogo):
    fixture = jogo["fixture"]
    league = jogo["league"]
    teams = jogo["teams"]

    casa = teams["home"]
    fora = teams["away"]
    season = league.get("season", datetime.now(FUSO).year)

    log(f"⚽ {casa['name']} x {fora['name']}")

    hist_casa = buscar_historico(casa["id"], season)
    time.sleep(1.5)
    hist_fora = buscar_historico(fora["id"], season)

    m_casa = metricas_ht(hist_casa)
    m_fora = metricas_ht(hist_fora)
    indice = calcular_indice_ht(m_casa, m_fora)

    return {
        "fixture_id": fixture["id"],
        "jogo": jogo,
        "casa": casa["name"],
        "fora": fora["name"],
        "liga": league.get("name"),
        "indice_ht": indice,
        "casa_ht": m_casa,
        "fora_ht": m_fora,
        "status": fixture["status"]["short"],
        "data_api": fixture.get("date"),
    }


def mensagem_pre_jogo(item):
    return (
        "📊 RELATÓRIO ESTATÍSTICO — PRIMEIRO TEMPO\n\n"
        f"⚽ {item['casa']} x {item['fora']}\n"
        f"🏆 {item['liga']}\n"
        f"🕒 {horario_brasilia(item.get('data_api'))}\n\n"
        f"🔥 Índice HT: {item['indice_ht']}/100\n\n"
        f"🏠 {item['casa']}\n"
        f"Gol HT: {item['casa_ht']['gol_ht']}%\n"
        f"Média HT: {item['casa_ht']['media']}\n\n"
        f"✈️ {item['fora']}\n"
        f"Gol HT: {item['fora_ht']['gol_ht']}%\n"
        f"Média HT: {item['fora_ht']['media']}\n\n"
        "ℹ️ Relatório estatístico informativo."
    )


def numero_stats(stats, nome):
    for item in stats:
        if item.get("type") == nome:
            valor = item.get("value")
            if valor is None:
                return 0
            try:
                return float(str(valor).replace("%", ""))
            except Exception:
                return 0
    return 0


def buscar_live(fixture_id):
    jogos = api_get("fixtures", {"id": fixture_id})
    if not jogos:
        return None

    jogo = jogos[0]
    status = jogo["fixture"]["status"]["short"]
    minuto = jogo["fixture"]["status"]["elapsed"] or 0

    resultado = {
        "fixture_id": fixture_id,
        "status": status,
        "minuto": minuto,
        "casa": jogo["teams"]["home"]["name"],
        "fora": jogo["teams"]["away"]["name"],
        "gols_casa": jogo["goals"]["home"] or 0,
        "gols_fora": jogo["goals"]["away"] or 0,
        "finalizacoes": 0,
        "no_alvo": 0,
        "escanteios": 0,
    }

    if status not in {"1H", "HT", "2H", "LIVE"}:
        return resultado

    stats = api_get("fixtures/statistics", {"fixture": fixture_id})
    if not stats:
        return resultado

    for equipe in stats:
        lista = equipe.get("statistics", [])
        resultado["finalizacoes"] += numero_stats(lista, "Total Shots")
        resultado["no_alvo"] += numero_stats(lista, "Shots on Goal")
        resultado["escanteios"] += numero_stats(lista, "Corner Kicks")

    return resultado


def identificar_marco(dados):
    minuto = dados["minuto"]
    status = dados["status"]

    if status == "HT":
        return "HT"
    if status == "FT":
        return "FT"

    for marco in [15, 30, 60, 75]:
        if marco <= minuto < marco + 3:
            return str(marco)

    return None


def mensagem_live(dados):
    return (
        "📡 ATUALIZAÇÃO ESTATÍSTICA AO VIVO\n\n"
        f"⚽ {dados['casa']} x {dados['fora']}\n"
        f"⏱️ Minuto: {dados['minuto']}'\n"
        f"🥅 Placar: {dados['gols_casa']} x {dados['gols_fora']}\n\n"
        f"🎯 Finalizações: {int(dados['finalizacoes'])}\n"
        f"🥅 No alvo: {int(dados['no_alvo'])}\n"
        f"🚩 Escanteios: {int(dados['escanteios'])}\n\n"
        "ℹ️ Atualização estatística informativa."
    )


def executar_scan_diario():
    global SELECIONADOS_ATUAIS

    CACHE_HISTORICO.clear()
    jogos = buscar_jogos_do_dia()

    if not jogos:
        log("⚠️ Nenhum jogo disponível para análise.")
        SELECIONADOS_ATUAIS = []
        return

    jogos = jogos[:CONFIG["max_jogos_analisar"]]
    analisados = []

    for numero, jogo in enumerate(jogos, start=1):
        log(f"📊 Análise {numero}/{len(jogos)}")
        item = analisar_jogo(jogo)
        analisados.append(item)
        time.sleep(2)

    ranking = sorted(analisados, key=lambda x: x["indice_ht"], reverse=True)

    selecionados = [
        x for x in ranking
        if x["indice_ht"] >= CONFIG["indice_ht_minimo"]
    ][:CONFIG["top_alertas"]]

    SELECIONADOS_ATUAIS = selecionados
    log(f"✅ Selecionados: {len(selecionados)}")

    for item in selecionados:
        telegram(mensagem_pre_jogo(item))
        time.sleep(1)


def deve_monitorar_agora(item):
    data_api = item.get("data_api")
    if not data_api:
        return False

    try:
        inicio = datetime.fromisoformat(
            data_api.replace("Z", "+00:00")
        ).astimezone(FUSO)

        agora = datetime.now(FUSO)

        inicio_janela = inicio - timedelta(
            minutes=CONFIG["janela_antes_inicio_min"]
        )
        fim_janela = inicio + timedelta(
            minutes=CONFIG["janela_depois_inicio_min"]
        )

        return inicio_janela <= agora <= fim_janela
    except Exception:
        return False


def monitorar_selecionados_uma_vez():
    for item in list(SELECIONADOS_ATUAIS):
        if not deve_monitorar_agora(item):
            continue

        dados = buscar_live(item["fixture_id"])
        if not dados:
            continue

        log(
            f"📡 {dados['casa']} x {dados['fora']} | "
            f"{dados['status']} | {dados['minuto']}' | "
            f"{dados['gols_casa']}x{dados['gols_fora']}"
        )

        marco = identificar_marco(dados)
        if not marco:
            continue

        chave = (dados["fixture_id"], marco)
        if chave in MARCOS_ENVIADOS:
            continue

        if telegram(mensagem_live(dados)):
            MARCOS_ENVIADOS.add(chave)


def chegou_hora_scan():
    agora = datetime.now(FUSO)
    return (
        agora.hour == CONFIG["hora_scan"]
        and agora.minute >= CONFIG["minuto_scan"]
    )


def main():
    global ULTIMA_DATA_SCAN

    validar_configuracao()
    log("🤖 Serviço iniciado.")

    if not testar_conexoes():
        log("⚠️ Conexões incompletas. O serviço continuará tentando.")

    while True:
        try:
            agora = datetime.now(FUSO)
            hoje = agora.strftime("%Y-%m-%d")

            if (
                ULTIMA_DATA_SCAN != hoje
                and (
                    chegou_hora_scan()
                    or agora.hour > CONFIG["hora_scan"]
                )
            ):
                log("🔎 Iniciando scan diário.")
                executar_scan_diario()
                ULTIMA_DATA_SCAN = hoje

            if SELECIONADOS_ATUAIS:
                monitorar_selecionados_uma_vez()

            time.sleep(CONFIG["intervalo_monitor"])

        except KeyboardInterrupt:
            log("⏹️ Serviço encerrado manualmente.")
            break

        except Exception as erro:
            log(f"❌ Erro no loop principal: {erro}")
            time.sleep(60)


if __name__ == "__main__":
    main()
