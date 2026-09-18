"""Route modules, one package per domain. Each domain's routes.py registers
its routes on the single shared Flask app via @app.route, imported into
app.py for that side effect. Endpoint names are unchanged from before this
split, so every url_for(...) call site elsewhere in the codebase (Python and
templates) keeps working without modification.
"""
