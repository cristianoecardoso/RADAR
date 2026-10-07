"""Fábrica da aplicação RADAR FNAC."""

from __future__ import annotations

from flask import Flask, jsonify, request

from radar_backend.radar_util import agora_iso, garantir_pastas


def create_app() -> Flask:
    app = Flask(__name__)

    # ZIPs/planilhas grandes podem ser enviados pelo TG. Limite local de 1 GiB.
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024

    garantir_pastas()

    from radar_backend.radar_core import bp as core_bp
    from radar_backend.tg_orcamento import bp as tg_bp
    from radar_backend.govted import bp as govted_bp
    from radar_backend.dinv import bp as dinv_bp
    from radar_backend.apigov import bp as apigov_bp
    from radar_backend.tc_gantt_empenho import bp as tc_gantt_bp
    from radar_backend.gerencial import bp as gerencial_bp
    from radar_backend.aeroportos import bp as aeroportos_bp
    from radar_backend.instrumentos_edicao import bp as edicao_bp
    from radar_backend.arquivos_db import bp as arquivos_bp
    from radar_backend.plano_trabalho import bp as pt_bp
    from radar_backend.tg_drive import bp as tg_drive_bp
    from radar_backend.batimento import bp as batimento_bp
    from radar_backend.checklist import bp as checklist_bp
    from radar_backend.preferencias import bp as preferencias_bp
    from radar_backend.bndes import bp as bndes_bp

    for blueprint in (core_bp, tg_bp, govted_bp, dinv_bp, apigov_bp, tc_gantt_bp, gerencial_bp, aeroportos_bp, edicao_bp, arquivos_bp, pt_bp, tg_drive_bp, batimento_bp, checklist_bp, preferencias_bp, bndes_bp):
        app.register_blueprint(blueprint)

    @app.errorhandler(404)
    def erro_404(exc):
        return jsonify({
            "sucesso": False,
            "erro": "Rota não encontrada no backend.",
            "rota": request.path,
            "tipo": "HTTP404",
            "timestamp": agora_iso(),
        }), 404

    @app.errorhandler(413)
    def erro_413(exc):
        return jsonify({
            "sucesso": False,
            "erro": "O arquivo enviado excedeu o limite aceito pelo backend.",
            "limite_bytes": app.config.get("MAX_CONTENT_LENGTH"),
            "tipo": "HTTP413",
            "timestamp": agora_iso(),
        }), 413

    @app.errorhandler(500)
    def erro_500(exc):
        return jsonify({
            "sucesso": False,
            "erro": str(getattr(exc, "original_exception", None) or exc),
            "tipo": "HTTP500",
            "timestamp": agora_iso(),
        }), 500

    return app
