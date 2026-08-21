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
    },
    TaskState.BLOCKED: {
        TaskState.EXECUTING,
        TaskState.HUMAN_REQUIRED,
    },
    TaskState.HUMAN_REQUIRED: {
        TaskState.EXECUTING,
        TaskState.BLOCKED,
    },
    TaskState.DONE: set(),
}


def is_legal_transition(from_state: TaskState, to_state: TaskState) -> bool:
    return to_state in LEGAL_TRANSITIONS.get(from_state, set())
