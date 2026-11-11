import sys

if __name__ == '__main__':
    # The packaged backend is also the simulated robot's process: brainos starts
    # ``brainos-server --nx-sim-child serve --adapter sim_quadruped ...`` (see
    # brainos/runtime_bridge.py), so the app needs no Python installation.
    if len(sys.argv) > 1 and sys.argv[1] == '--nx-sim-child':
        from brainos.sim_child import main as sim_child
        sys.exit(sim_child(sys.argv[2:]))
    from brainos.server import main
    main()
