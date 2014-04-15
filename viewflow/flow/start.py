"""
Start flow task, instantiate new flow process
"""
import warnings
from inspect import isfunction

from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.views.generic.edit import UpdateView

from viewflow.activation import StartActivation
from viewflow.exceptions import FlowRuntimeError
from viewflow.flow.base import Event, Edge


def flow_start_view():
    """
    Decorator for start views, creates and initializes start activation
    Expects view with signature :: (request, activation, **kwargs)
    Returns                     :: (request, flow_task, **kwargs)
    """
    class StartViewDecorator(object):
        def __init__(self, func, activation=None):
            self.func = func
            self.activation = activation

        def __call__(self, request, flow_task, **kwargs):
            if self.activation and flow_task.activation_cls:
                warnings.warn('View already implemens StartActivation interface. '
                              'Flow task `{}` activation_cls ignored'.format(flow_task.name),
                              RuntimeWarning)

            if self.activation:
                self.activation.initialize(flow_task)
                return self.func(request, **kwargs)
            else:
                activation = flow_task.activation_cls()
                activation.initialize(flow_task)
                return self.func(request, activation, **kwargs)

        def __get__(self, instance, instancetype):
            """
            If we decoration method on CBV that have StartActivation interface,
            no custom activation required
            """
            if instance is None:
                return self

            func = self.func.__get__(instance, type)
            activation = instance if isinstance(instance, StartActivation) else None

            return self.__class__(func, activation=activation)

    return StartViewDecorator


class StartViewActivation(StartActivation):
    """
    Tracks task statistics in activation form
    """
    management_form_cls = None

    def __init__(self, management_form_cls=None, **kwargs):
        super(StartViewActivation, self).__init__(**kwargs)
        self.management_form = None
        if management_form_cls:
            self.management_form_cls = management_form_cls

    def get_management_form_cls(self):
        if self.management_form_cls:
            return self.management_form_cls
        else:
            return self.flow_cls.management_form_cls

    def prepare(self, data=None):
        super(StartViewActivation, self).prepare()

        management_form_cls = self.get_management_form_cls()
        self.management_form = management_form_cls(data=data, instance=self.task)

        if data:
            if not self.management_form.is_valid():
                raise FlowRuntimeError('Activation metadata is broken {}'.format(self.management_form.errors))
            self.task = self.management_form.save(commit=False)


class StartView(StartViewActivation, UpdateView):
    fields = []

    @property
    def model(self):
        return self.flow_cls.process_cls

    def get_context_data(self, **kwargs):
        context = super(StartView, self).get_context_data(**kwargs)
        context['activation'] = self
        return context

    def get_object(self):
        return self.process

    def get_template_names(self):
        return ('{}/flow/start.html'.format(self.flow_cls._meta.app_label),
                'viewflow/flow/start.html')

    def get_success_url(self):
        return reverse('viewflow:index', current_app=self.flow_cls._meta.app_label)

    def form_valid(self, form):
        response = super(StartView, self).form_valid(form)
        self.done(process=self.object)
        return response

    @flow_start_view()
    def dispatch(self, request, *args, **kwargs):
        if not self.flow_task.has_perm(request.user):
            raise PermissionDenied

        self.prepare(request.POST or None)
        return super(StartView, self).dispatch(request, *args, **kwargs)


class Start(Event):
    """
    Start process event
    """
    task_type = 'START'
    activation_cls = StartViewActivation

    def __init__(self, view_or_cls=None, activation_cls=None, **kwargs):
        """
        Accepts view callable or CBV View class with view kwargs,
        if CBV view implements StartActivation, it used as activation_cls
        """
        self._view, self._view_cls, self._view_args = None, None, None

        if isfunction(view_or_cls):
            self._view = view_or_cls
        elif view_or_cls is not None:
            self._view_cls = view_or_cls
            self._view_args = kwargs
            if issubclass(view_or_cls, StartActivation):
                activation_cls = view_or_cls

        super(Start, self).__init__(activation_cls=activation_cls)

        self._activate_next = []
        self._owner = None
        self._owner_permission = None

    def _outgoing(self):
        for next_node in self._activate_next:
            yield Edge(src=self, dst=next_node, edge_class='next')

    def activate_next(self, self_activation, **kwargs):
        """
        Activate all outgoing edges
        """
        for outgoing in self._outgoing():
            outgoing.dst.activate(prev_activation=self_activation)

    def Activate(self, node):
        self._activate_next.append(node)
        return self

    def Available(self, owner=None, **owner_kwargs):
        """
        Make process start action available for the User
        accepts user lookup kwargs or callable predicate :: User -> bool

        .Available(username='employee')
        .Available(lambda user: user.is_super_user)
        """
        if owner:
            self._owner = owner
        else:
            self._owner = owner_kwargs
        return self

    def Permission(self, permission, assign_view=None):
        """
        Make process start available for users with specific permission
        acceps permissions name or callable predicate :: User -> bool

        .Permission('my_app.can_approve')
        .Permission(lambda user: user.department_id is not None)
        """
        self._owner_permission = permission
        self._assign_view = assign_view
        return self

    @property
    def view(self):
        if not self._view:
            if not self._view_cls:
                from viewflow.views import start
                return start
            else:
                self._view = self._view_cls(self._view_args)
                return self._view
        return self._view

    def has_perm(self, user):
        from django.contrib.auth import get_user_model

        if self._owner:
            if callable(self._owner) and self._owner(user):
                return True
            owner = get_user_model()._default_manager.get(**self._owner)
            return owner == user

        elif self._owner_permission:
            if callable(self._owner_permission) and self._owner_permission(user):
                return True
            return user.has_perm(self._owner_permission)

        else:
            """
            No restriction
            """
            return True
