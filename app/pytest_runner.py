"""Exécute réellement pytest dans la racine d'un projet cible et construit
un TestResult structuré. Utilise --junitxml (sortie structurée officielle
de pytest) plutôt que de parser du texte humain — plus robuste, rien
d'inventé.

Statuts :
- NOT_EXECUTED : pytest n'est pas installé/importable dans l'environnement
  utilisé pour lancer le projet cible.
- UNAVAILABLE  : pytest s'est exécuté mais n'a trouvé aucun test.
- VERIFIED_PASS / VERIFIED_FAIL : pytest s'est exécuté, des tests
  existent, tous ont réussi ou au moins un a échoué/erreuré.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from app.models import TestCaseOutcome, TestCaseResult, TestResult, TestResultStatus

NOT_EXECUTED_MARKERS = ("No module named pytest", "No module named 'pytest'")


def _parse_junit(junit_path: Path) -> tuple[int, int, int, int, list[TestCaseResult]]:
    """Retourne (total, failed, errors, skipped, test_cases) depuis le
    rapport JUnit XML réellement produit par pytest."""
    tree = ET.parse(junit_path)
    root = tree.getroot()

    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))

    total = failed = errors = skipped = 0
    test_cases: list[TestCaseResult] = []

    for suite in suites:
        for case in suite.findall("testcase"):
            total += 1
            classname = case.get("classname", "")
            name = case.get("name", "")
            full_name = f"{classname}::{name}" if classname else name

            if case.find("failure") is not None:
                outcome = TestCaseOutcome.FAILED
                failed += 1
            elif case.find("error") is not None:
                outcome = TestCaseOutcome.ERROR
                errors += 1
            elif case.find("skipped") is not None:
                outcome = TestCaseOutcome.SKIPPED
                skipped += 1
            else:
                outcome = TestCaseOutcome.PASSED

            test_cases.append(TestCaseResult(name=full_name, outcome=outcome))

    return total, failed, errors, skipped, test_cases


def run_pytest_for_project(project_path: str | Path, task_id: str, max_output_chars: int = 8000) -> TestResult:
    project_path = Path(project_path)

    if not project_path.exists() or not project_path.is_dir():
        return TestResult(
            task_id=task_id,
            status=TestResultStatus.NOT_EXECUTED,
            raw_output=f"répertoire de projet introuvable: {project_path}",
        )

    # Ceinture et bretelles vis-à-vis du même problème de cache bytecode :
    # supprime tout __pycache__ résiduel d'un run précédent avant de lancer.
    for cache_dir in project_path.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)

    junit_fd, junit_name = tempfile.mkstemp(suffix=".xml", prefix="herbert_junit_")
    junit_path = Path(junit_name)
    os.close(junit_fd)

    # PYTHONDONTWRITEBYTECODE : un projet cible peut être testé plusieurs
    # fois de suite après modification de son code. Sans ça, un .pyc mis en
    # cache par un run précédent peut être réutilisé à tort si le fichier
    # réécrit a la même taille et un mtime à la même seconde (constaté
    # réellement pendant la mise au point du test de régression).
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    try:
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", ".", f"--junitxml={junit_path}", "-q"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )
        except (FileNotFoundError, OSError) as exc:
            return TestResult(
                task_id=task_id,
                status=TestResultStatus.NOT_EXECUTED,
                raw_output=f"impossible de lancer pytest: {exc}",
            )
        duration = time.perf_counter() - start

        raw_output = (proc.stdout + "\n" + proc.stderr).strip()
        truncated_output = raw_output[:max_output_chars]

        if any(marker in raw_output for marker in NOT_EXECUTED_MARKERS):
            return TestResult(
                task_id=task_id,
                status=TestResultStatus.NOT_EXECUTED,
                duration_seconds=duration,
                raw_output=truncated_output,
            )

        if not junit_path.exists() or junit_path.stat().st_size == 0:
            # pytest n'a pas produit de rapport exploitable (ex: erreur de
            # configuration avant même la collecte) — pas une invention de
            # résultat, on le rapporte tel quel comme NOT_EXECUTED.
            return TestResult(
                task_id=task_id,
                status=TestResultStatus.NOT_EXECUTED,
                duration_seconds=duration,
                raw_output=truncated_output,
            )

        total, failed, errors, skipped, test_cases = _parse_junit(junit_path)

        if total == 0:
            return TestResult(
                task_id=task_id,
                status=TestResultStatus.UNAVAILABLE,
                total=0,
                duration_seconds=duration,
                raw_output=truncated_output,
                test_cases=test_cases,
            )

        passed = total - failed - errors - skipped
        status = TestResultStatus.VERIFIED_FAIL if (failed or errors) else TestResultStatus.VERIFIED_PASS

        return TestResult(
            task_id=task_id,
            status=status,
            total=total,
            passed=passed,
            failed=failed,
            errors=errors,
            duration_seconds=duration,
            raw_output=truncated_output,
            test_cases=test_cases,
        )
    finally:
        junit_path.unlink(missing_ok=True)
