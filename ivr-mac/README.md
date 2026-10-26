# ivr-mac

Phone side of the LangBang IVR, runs on the Mac mini (`~/projects/ivr-mac`):
`ivr_answer.py` (launchd `com.tyrel.ivr-answer`) answers Google Voice over
CDP and hands the call to `ivr_loop.py`, which talks to the IVR node
(`server/ivr_node.py`, :8130) over BlackHole audio.

The Mac has no git: this folder is the source of truth, synced with rsync.

    # repo -> Mac (deploy)
    rsync -a --exclude-from=ivr-mac/.gitignore ivr-mac/ tyrels-mac-mini:projects/ivr-mac/
    # Mac -> repo (after editing on the Mac)
    rsync -a --exclude-from=ivr-mac/.gitignore --exclude=launchd tyrels-mac-mini:projects/ivr-mac/ ivr-mac/

`launchd/` holds copies of the LaunchAgents with personal values replaced
(`YOUR_NUMBERS`, `YOUR_HUB_HOST`); the live ones are in `~/Library/LaunchAgents`.

Regression suite (run before and after any IVR change):

    .venv/bin/python tests/run_suite.py [logic] [convo] [history] [audio]

`tests/build_cases.py` rebuilds the history and audio cases from the
recorded calls; those stay on the Mac (real callers).
