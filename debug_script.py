import sys
print("step 1: starting file read", flush=True)

try:
    with open(r"campaign_db_scripts/02_sync_campaigns_adgroups.py", encoding="utf-8") as f:
        content = f.read()
    print(f"step 2: file read ok — {len(content):,} chars, {content.count(chr(10)):,} lines", flush=True)
except Exception as e:
    print(f"FAILED reading file: {e}", flush=True)
    sys.exit(1)

print("step 3: checking first 200 chars of file:", flush=True)
print(repr(content[:200]), flush=True)

print("\nstep 4: checking if __name__ == '__main__' block exists:", flush=True)
print("has main guard:", "if __name__" in content, flush=True)
print("has def main():", "def main():" in content, flush=True)
print("has first print:", 'print("=' in content, flush=True)

print("\nstep 5: compiling file", flush=True)
try:
    code = compile(content, "02_sync_campaigns_adgroups.py", "exec")
    print("step 6: compile ok", flush=True)
except SyntaxError as e:
    print(f"SYNTAX ERROR at line {e.lineno}: {e.msg}", flush=True)
    sys.exit(1)

print("\nstep 7: executing file in isolated namespace", flush=True)
namespace = {"__name__": "__not_main__"}  # prevents main() from being called
try:
    exec(code, namespace)
    print("step 8: module-level exec ok", flush=True)
    print(f"  functions defined: {[k for k in namespace if callable(namespace[k]) and not k.startswith('__')]}", flush=True)
except SystemExit as e:
    print(f"SYSTEM EXIT during module load: code={e.code}", flush=True)
except Exception as e:
    print(f"EXCEPTION during module load: {type(e).__name__}: {e}", flush=True)

print("\nstep 9: calling main() directly", flush=True)
if "main" in namespace:
    try:
        namespace["main"]()
    except SystemExit as e:
        print(f"SYSTEM EXIT from main(): code={e.code}", flush=True)
    except Exception as e:
        print(f"EXCEPTION from main(): {type(e).__name__}: {e}", flush=True)
else:
    print("ERROR: main() not found in namespace", flush=True)
