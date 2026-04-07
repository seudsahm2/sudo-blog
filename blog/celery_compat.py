try:
    from celery import shared_task as celery_shared_task
except ImportError:  # pragma: no cover
    celery_shared_task = None


def shared_task(*decorator_args, **decorator_kwargs):
    if celery_shared_task is not None:
        return celery_shared_task(*decorator_args, **decorator_kwargs)

    def decorator(func):
        return func

    if decorator_args and callable(decorator_args[0]) and not decorator_kwargs:
        return decorator(decorator_args[0])
    return decorator
