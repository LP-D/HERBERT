from app.state_machine.states import TaskState
from app.state_machine.transitions import LEGAL_TRANSITIONS, is_legal_transition

# NOTE: app.state_machine.service n'est volontairement PAS importé ici.
# service.py dépend de app.database.repository, qui dépend de app.models,
# qui dépend de app.state_machine.states -> l'importer au niveau du package
# créerait un cycle d'import. Utiliser `from app.state_machine.service
# import transition_task, TaskNotFoundError` directement.

__all__ = ["TaskState", "LEGAL_TRANSITIONS", "is_legal_transition"]
