import os
import configparser


def resolve_work_dir() -> str:
    """
    Resolve the work directory.

    Priority:
    1) WORK_DIR env var
    2) config.ini (repo root) [MAIN].work_dir
    3) repo root
    """
    env_path = os.getenv("WORK_DIR")
    if env_path:
        try:
            os.makedirs(env_path, exist_ok=True)
        except Exception:
            pass
        return env_path

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    config_path = os.path.join(repo_root, "config.ini")
    if os.path.exists(config_path):
        cfg = configparser.ConfigParser()
        cfg.read(config_path)
        try:
            cfg_path = cfg.get("MAIN", "work_dir", fallback=None)
        except Exception:
            cfg_path = None
        if cfg_path:
            try:
                os.makedirs(cfg_path, exist_ok=True)
            except Exception:
                pass
            return cfg_path

    return repo_root
