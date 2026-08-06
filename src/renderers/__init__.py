from .meme import render_job as render_meme_job
from .ranking import render_job as render_ranking_job


def render_job(cfg, job_id: str):
    if cfg.active_template == "meme":
        return render_meme_job(cfg, job_id)
    if cfg.active_template == "ranking":
        return render_ranking_job(cfg, job_id)
    raise RuntimeError(f"No renderer implemented for template: {cfg.active_template}")
