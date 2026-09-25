from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ollama_base_url: str = "http://127.0.0.1:11434"
    # Pasif gözlem için sık örnekleme
    poll_interval_sec: float = 0.4
    history_size: int = 120
    host: str = "0.0.0.0"
    port: int = 8080
    broadcast_min_interval_ms: float = 200.0
    # Runner CPU eşiği (Linux multicore %, ham) — üstündeyse busy
    runner_cpu_active_pct: float = 8.0
    # expires_at ileri kayarsa kullanım nabzı
    expires_skew_sec: float = 1.0
    # llama-server /slots host (boşsa ollama_base_url hostname)
    slots_host: str = ""
    # Virgülle ayrılmış bilinen /slots portları (Docker Desktop Mac için)
    slots_ports: str = ""
    # Host'un yazdığı port listesi dosyası (örn. /app/.slots-ports)
    slots_ports_file: str = ""
    # /proc'ta runner yoksa host'ta /slots portlarını tara (yavaş; Mac'te file tercih)
    slots_discover: bool = False
    # Keşif taraması aralığı (dahil); örn. 30000-65535
    slots_scan_start: int = 30000
    slots_scan_end: int = 65535
    # /proc kökü (Docker'da /host/proc bağlanabilir; boşsa /proc)
    proc_root: str = "/proc"

    class Config:
        env_prefix = "LLMPERF_"


settings = Settings()
