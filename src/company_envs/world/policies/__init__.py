"""Worker policy adapters selected by ``[worker_policy] policies`` in config.toml.

The controller runs one no-hint episode per configured policy after the teacher passes.
``"own"`` is our WorkerPolicy (company_envs.world.backends.mypcbench); any other name ``x``
is imported lazily as ``company_envs.world.policies.x`` and must define ``XPolicy`` with
WorkerPolicy's constructor ``(config, directory, *, role_note="", models=None, reserve=None)``
and its call signature: ``await policy(observation)`` returns a harness Action. ``reserve``
charges the controller's model-call budget and must be called before every provider call.
A missing adapter only fails the run that selects it.
"""
