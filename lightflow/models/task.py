from .action import Action
from .task_data import MultiTaskData
from .exceptions import TaskReturnActionInvalid, AbortWorkflow, StopTask
from lightflow.queue import JobType


class TaskState:
    """ Constants for flagging the current state of the task. """
    Init = 1
    Waiting = 2
    Running = 3
    Completed = 4
    Stopped = 5
    Aborted = 6


class BaseTask:
    """ The base class for all tasks.

    Tasks should inherit from this class and implement the run() method.
    """
    def __init__(self, name, *, queue=JobType.Task, force_run=False, propagate_skip=True):
        """ Initialize the base task.

        The dag_name and workflow_name attributes are filled at runtime.

        Args:
            name (str): The name of the task.
            queue (str): Name of the queue the task should be scheduled to.
            force_run (bool): Run the task even if it is flagged to be skipped.
            propagate_skip (bool): Propagate the skip flag to the next task.
        """
        self._name = name
        self._queue = queue
        self._force_run = force_run
        self._propagate_skip = propagate_skip

        self._skip = False
        self._state = TaskState.Init
        self._celery_result = None

        self.workflow_name = None
        self.dag_name = None

    @property
    def name(self):
        """ Returns the name of the task. """
        return self._name

    @property
    def queue(self):
        """ Returns the queue the task should be scheduled to. """
        return self._queue

    @property
    def has_to_run(self):
        """ Returns whether the task has to run, even if the DAG would skip it. """
        return self._force_run

    @property
    def propagate_skip(self):
        """ Returns whether the skip flag should be propagated to the successor tasks. """
        return self._propagate_skip

    @property
    def is_waiting(self):
        """ Internal state: returns whether the task is waiting in the DAG to be run. """
        return self._state == TaskState.Waiting

    @property
    def is_running(self):
        """ Internal state: returns whether the task is currently running. """
        return self._state == TaskState.Running

    @property
    def is_completed(self):
        """ Internal state: returns whether the task has completed successfully. """
        return self._state == TaskState.Completed

    @property
    def is_stopped(self):
        """ Internal state: returns whether the task was stopped. """
        return self._state == TaskState.Stopped

    @property
    def is_aborted(self):
        """ Internal state: returns whether the task was aborted. """
        return self._state == TaskState.Aborted

    @property
    def is_skipped(self):
        """ Internal state: returns whether the task was skipped. """
        return self._skip

    @is_skipped.setter
    def is_skipped(self, value):
        """ Set whether the task has been skipped.

        Args:
            value (bool): Set to True if the tasked was skipped.
        """
        self._skip = value

    @property
    def state(self):
        """ Returns the internal state of the task. """
        return self._state

    @state.setter
    def state(self, state):
        """ Sets the internal state of the task.
        
        Args:
            state (TaskState): The new state of the task
        """
        self._state = state

    @property
    def celery_pending(self):
        """ Celery state: returns whether the task is queued. """
        if self.has_celery_result:
            return self.celery_result.state == "PENDING"
        else:
            return False

    @property
    def celery_completed(self):
        """ Celery state: returns whether the execution of the task has completed. """
        if self.has_celery_result:
            return self.celery_result.ready()
        else:
            return False

    @property
    def celery_failed(self):
        """ Celery state: returns whether the execution of the task failed. """
        if self.has_celery_result:
            return self.celery_result.failed()
        else:
            return False

    @property
    def celery_state(self):
        """ Returns the current celery state of the task as a string. """
        if self.has_celery_result:
            return self.celery_result.state
        else:
            return "NOT_QUEUED"

    @property
    def has_celery_result(self):
        """ Returns whether the task has a result from celery.

        This indicates that the task is either queued, running or finished.
        """
        return self.celery_result is not None

    @property
    def celery_result(self):
        """ Returns the celery result object for this task. """
        return self._celery_result

    @celery_result.setter
    def celery_result(self, result):
        """ Sets the celery result object for this task.

        Args:
            result (AsyncResult): The result of the celery queuing call.
        """
        self._celery_result = result

    def clear_celery_result(self):
        """ Removes the task's celery result from the result backend. """
        if self.has_celery_result:
            self._celery_result.forget()

    def _run(self, data, store, signal, context, *,
             success_callback=None, stop_callback=None, abort_callback=None):
        """ The internal run method that decorates the public run method.

        This method makes sure data is being passed to and from the task.

        Args:
            data (MultiTaskData): The data object that has been passed from the
                                  predecessor task.
            store (DataStoreDocument): The persistent data store object that allows the
                                       task to store data for access across the current
                                       workflow run.
            signal (TaskSignal): The signal object for tasks. It wraps the construction
                                 and sending of signals into easy to use methods.
            context (TaskContext): The context in which the tasks runs.
            success_callback: This function is called when the task completed successfully
            stop_callback: This function is called when a StopTask exception was raised.
            abort_callback: This function is called when an AbortWorkflow exception
                            was raised.

        Raises:
            TaskReturnActionInvalid: If the return value of the task is not
                                     an Action object.

        Returns:
            Action: An Action object containing the data that should be passed on
                    to the next task and optionally a list of successor tasks that
                    should be executed.
        """
        if data is None:
            data = MultiTaskData(self._name)

        try:
            result = self.run(data, store, signal, context)

            if success_callback is not None:
                success_callback()

        # the task should be stopped and optionally all successor tasks skipped
        except StopTask as err:
            if stop_callback is not None:
                stop_callback(exc=err)

            result = Action(data, limit=[]) if err.skip_successors else None

        # the workflow should be stopped immediately
        except AbortWorkflow as err:
            if abort_callback is not None:
                abort_callback(exc=err)

            result = None
            signal.stop_workflow()

        if result is None:
            return Action(data)
        else:
            if not isinstance(result, Action):
                raise TaskReturnActionInvalid()

            result.data.add_task_history(self.name)
            return result

    def run(self, data, store, signal, context, **kwargs):
        """ The main run method of a task.

        Implement this method in inherited classes.

        Args:
            data (MultiTaskData): The data object that has been passed from the
                                  predecessor task.
            store (DataStoreDocument): The persistent data store object that allows the
                                       task to store data for access across the current
                                       workflow run.
            signal (TaskSignal): The signal object for tasks. It wraps the construction
                                 and sending of signals into easy to use methods.
            context (TaskContext): The context in which the tasks runs.

        Returns:
            Action: An Action object containing the data that should be passed on
                    to the next task and optionally a list of successor tasks that
                    should be executed.
        """
        pass
