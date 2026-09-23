"""Connector packages. A regular package, not a namespace one: without this file
pytest puts `connectors/` itself on sys.path when collecting a connector's tests,
and `import trino` inside the trino connector then finds this repository's
`connectors/trino` instead of the Trino client.
"""
