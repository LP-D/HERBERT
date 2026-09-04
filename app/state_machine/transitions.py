from app.state_machine.states import TaskState

# Table explicite des transitions légales. Toute paire (from, to) absente
# de cet ensemble est refusée par is_legal_transition().
LEGAL_TRANSITIONS = {
    TaskState.RECEIVED: {TaskState.EXECUTING, TaskState.BLOCKED},
    TaskState.EXECUTING: {
        TaskState.TESTING,
        TaskState.FAILED,
        TaskState.BLOCKED,
        TaskState.HUMAN_REQUIRED,
    },
    TaskState.TESTING: {
        TaskState.DONE,
        TaskState.FAILED,
        TaskState.EXECUTING,
    },
    TaskState.FAILED: {
        TaskState.EXECUTING,
        TaskState.HUMAN_REQUIRED,
        # BLOCKED (pivot orchestration headless, voir docs/DECISIONS.md) :
        # plafond de 3 itérations épuisé dans app/headless_orchestrator.py.
        # Absente jusqu'ici car FAILED n'était atteint que par un humain
        # (qui repart en EXECUTING ou escalade en HUMAN_REQUIRED) — la
        # boucle automatique a besoin d'un état visible distinct pour
        # signaler "budget de tentatives épuisé", jamais une 4e tentative
        # silencieuse.
        TaskState.BLOCKED,
    },
    TaskState.BLOCKED: {
        TaskState.EXECUTING,
        TaskState.HUMAN_REQUIRED,
    },
    TaskState.HUMAN_REQUIRED: {
        TaskState.EXECUTING,
        TaskState.BLOCKED,
    },
    # DONE -> PROMOTED : gate réel appliqué par `engine task promote` sur le
    # dernier ChangeProof (VERIFIED_PASS, sans régression), pas seulement
    # par cette table — voir cmd_task_promote.
    # DONE -> HUMAN_REQUIRED (pivot orchestration headless, voir
    # docs/DECISIONS.md) : après des tests passés, classify_push() peut
    # classer le diff MANUAL_REQUIRED (app/headless_orchestrator.py) — la
    # tâche reste DONE au sens "code fonctionnel", mais son passage à
    # PROMOTED/push nécessite une décision humaine, jamais automatique.
    TaskState.DONE: {TaskState.PROMOTED, TaskState.HUMAN_REQUIRED},
    # PROMOTED -> DONE : health check post-merge réussi.
    # PROMOTED -> ROLLED_BACK : health check en échec ET le revert du merge
    # a réellement réussi (AUTO_ROLLBACK) — voir cmd_task_promote. Si le
    # revert lui-même échoue (ROLLBACK_FAILED côté `promotions`), la tâche
    # reste volontairement à PROMOTED : le merge est toujours en place dans
    # le dépôt, donc l'état ne doit PAS afficher ROLLED_BACK tant que ce
    # n'est pas réellement le cas.
    TaskState.PROMOTED: {TaskState.DONE, TaskState.ROLLED_BACK},
    # ROLLED_BACK : terminal. Le merge a été annulé par un commit de revert
    # réel (jamais un reset destructeur) ; reprendre le travail nécessite
    # une nouvelle tâche/candidate, pas une transition depuis celle-ci.
    TaskState.ROLLED_BACK: set(),
}


def is_legal_transition(from_state: TaskState, to_state: TaskState) -> bool:
    return to_state in LEGAL_TRANSITIONS.get(from_state, set())
