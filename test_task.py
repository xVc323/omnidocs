import importlib

# First, try importing celery_app
print("1. Importing celery_app...")
try:
    from celery_app import celery_app
    print(f"Celery app imported: {celery_app}")
except Exception as e:
    print(f"Error importing celery_app: {e}")

# Second, try importing tasks module
print("\n2. Importing tasks module...")
try:
    import tasks
    print(f"Tasks module imported")
except Exception as e:
    print(f"Error importing tasks: {e}")

# Third, reload modules to make sure everything is registered
print("\n3. Reloading modules...")
try:
    importlib.reload(tasks)
    print("Tasks module reloaded")
except Exception as e:
    print(f"Error reloading tasks: {e}")

# Finally, check registered tasks
print("\n4. Checking registered tasks...")
try:
    print(f"Registered tasks: {celery_app.tasks}")
    print(f"\nLooking for 'tasks.process_site_task':")
    if 'tasks.process_site_task' in celery_app.tasks:
        print("FOUND! Task is properly registered.")
    else:
        print("NOT FOUND! Task is missing.")
        
    # Check all available tasks
    print("\nAll available tasks:")
    for task_name in celery_app.tasks:
        print(f"  - {task_name}")
except Exception as e:
    print(f"Error checking tasks: {e}") 