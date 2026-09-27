"""
Script to initialize demo-repo as an independent git repo with the exact
in-flight state expected by DevHandoff:
- Staged changes to models.py (WIP Tag model)
- Unstaged changes to notifications.py
- Untracked file middleware.py
- Multiple commits with issue refs (#42, #87)
"""
import os
import subprocess
import shutil

DEMO_DIR = os.path.abspath("demo-repo")

def run(cmd, cwd=DEMO_DIR):
    subprocess.run(cmd, cwd=cwd, check=True, shell=True)

def main():
    git_dir = os.path.join(DEMO_DIR, ".git")
    if os.path.exists(git_dir):
        shutil.rmtree(git_dir)

    print("1. Initializing git repo in demo-repo...")
    run("git init -b master")
    run('git config user.name "Alex Chen"')
    run('git config user.email "alex@taskflow.dev"')

    # Read current files to preserve
    with open(os.path.join(DEMO_DIR, "models.py"), "r") as f:
        full_models = f.read()
    with open(os.path.join(DEMO_DIR, "notifications.py"), "r") as f:
        full_notifications = f.read()

    # Base models.py without WIP Tag
    base_models = full_models.replace(
        '# WIP: adding tag support for tasks\nclass Tag(Base):\n    __tablename__ = "tags"\n    id = Column(Integer, primary_key=True)\n    name = Column(String, nullable=False)\n',
        ""
    )
    with open(os.path.join(DEMO_DIR, "models.py"), "w") as f:
        f.write(base_models)

    # Base notifications.py
    base_notifications = full_notifications.replace(
        '# TODO: wire up Celery for async notification queue\n',
        ""
    )
    with open(os.path.join(DEMO_DIR, "notifications.py"), "w") as f:
        f.write(base_notifications)

    # Temporarily move untracked middleware.py aside
    middleware_path = os.path.join(DEMO_DIR, "middleware.py")
    middleware_backup = None
    if os.path.exists(middleware_path):
        with open(middleware_path, "r") as f:
            middleware_backup = f.read()
        os.remove(middleware_path)

    # First commit
    run("git add .")
    run('git commit -m "feat(core): initial TaskFlow API skeleton and database models (#12)"')

    # Second commit
    run('git commit --allow-empty -m "feat(auth): implement user authentication and priority queue (#42)"')

    # Third commit
    run('git commit --allow-empty -m "refactor(api): add task CRUD endpoints and error handlers (#87)"')

    # Now stage changes to models.py (WIP Tag model)
    with open(os.path.join(DEMO_DIR, "models.py"), "w") as f:
        f.write(full_models)
    run("git add models.py")

    # Now create unstaged changes in notifications.py
    with open(os.path.join(DEMO_DIR, "notifications.py"), "w") as f:
        f.write(full_notifications)

    # Restore untracked middleware.py
    if middleware_backup:
        with open(middleware_path, "w") as f:
            f.write(middleware_backup)

    print("Demo repo initialized successfully with staged, unstaged, and untracked files!")

if __name__ == "__main__":
    main()
