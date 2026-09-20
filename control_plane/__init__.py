"""Brightstars Academics control plane: many schools, one deployment.

* ``models`` / ``registry`` - the platform registry (schools, their domains,
  platform admins) in its own database.
* ``resolver``              - picks the school from the request's hostname.
* ``routing``               - gives every school its own database.
* ``provisioning`` / ``cli`` - create and register schools.

Kept import-light on purpose: ``models.base`` imports ``control_plane.routing``
to build the session class, so nothing imported here may import ``models`` or
``app`` at module level.

Production database is PostgreSQL; see docs/architecture/MULTI_TENANCY.md.
"""
