# Goldens deprecados — M2.E

Estos goldens son del harness viejo `app/evals/runner.py` que testeaba
cada tool-call del LLM por separado. Cubrían la fase `inscripcion` que
antes usaba `tools_inscripcion.py` (eliminado en M2.E).

Desde M2.A/B/C/D/E (oct 2026), la fase de inscripción es 100%
determinística (`app/petra_agents/inscripcion_v3.py`). Los tests
end-to-end nuevos están en `app/evals_e2e/golden/`.

Los dejamos acá por referencia histórica, pero NO corren.
