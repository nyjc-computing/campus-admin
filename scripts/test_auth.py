#!/usr/bin/env python3
"""Test script to troubleshoot campus-admin authentication issues."""

import os
import sys

print("=" * 80)
print("ENVIRONMENT DIAGNOSTICS")
print("=" * 80)

# Check environment variables
print(f"\nENV: {os.environ.get('ENV', 'NOT SET')}")
print(f"CAMPUS_ENV: {os.environ.get('CAMPUS_ENV', 'NOT SET')}")
print(f"PORT: {os.environ.get('PORT', 'NOT SET')}")
print(f"CLIENT_ID: {os.environ.get('CLIENT_ID', 'NOT SET')}")
print(f"CLIENT_SECRET: {'SET' if os.environ.get('CLIENT_SECRET') else 'NOT SET'}")
print(f"DEPLOY: {os.environ.get('DEPLOY', 'NOT SET')}")

print("\n" + "=" * 80)
print("TESTING CAMPUS CLIENT INITIALIZATION")
print("=" * 80)

try:
    import campus_python
    campus = campus_python.Campus(timeout=60)
    print("\n✓ Campus client initialized successfully")
    
    # Check what URL will be used
    auth_client = campus.auth
    print("✓ Auth service client created")
    
    # Access the internal json_client to see base_url
    if hasattr(auth_client, '_json_client'):
        print(f"  Base URL: {auth_client._json_client.base_url}")
    
except Exception as e:
    print("\n✗ Failed to initialize Campus client:")
    print(f"  Error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n" + "=" * 80)
print("TESTING AUTH SERVICE CONNECTION")
print("=" * 80)

try:
    # Try a simple request to the auth service
    import requests
    
    # Get the base URL that would be used
    env_val = os.environ.get('ENV', os.environ.get('CAMPUS_ENV', 'development'))
    print(f"\nEnvironment detected: {env_val}")
    
    if env_val == "development":
        base_url = "https://campusauth-development.up.railway.app"
    elif env_val == "staging":
        base_url = "https://auth.campus.nyjc.dev"
    elif env_val == "production":
        base_url = "https://auth.campus.nyjc.app"
    else:
        base_url = f"http://localhost:{os.environ.get('PORT', '8080')}"
    
    print(f"Testing connection to: {base_url}")
    
    # Campus services expose their health check at the root URL via
    # campus.common.devops.deploy.configure_for_deployment; there is no
    # /health route. Don't assert on the body (unauthenticated metadata
    # endpoint - shape may change).
    response = requests.get(base_url, timeout=5)
    if response.status_code == 200:
        print(f"\n✓ Health check (GET /): {response.status_code}")
        print(f"  Body: {response.text[:200]}")
    else:
        print(f"\n✗ Health check (GET /): {response.status_code}")
    
except requests.exceptions.ConnectionError as e:
    print("\n✗ Connection failed:")
    print(f"  Error: {e}")
except Exception as e:
    print("\n✗ Unexpected error:")
    print(f"  Error: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 80)
print("TESTING FLASK APP INITIALIZATION")
print("=" * 80)

try:
    from apps.admin import create_app
    app = create_app()
    print("\n✓ Flask app created successfully")
    print(f"  App name: {app.name}")
    print(f"  Blueprints: {list(app.blueprints.keys())}")
    
except Exception as e:
    print("\n✗ Failed to create Flask app:")
    print(f"  Error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n" + "=" * 80)
print("TESTING AUTH ROUTES")
print("=" * 80)

try:
    with app.test_client() as client:
        # Test the login endpoint
        print("\nTesting GET /login?next=/dashboard")
        response = client.get('/login?next=/dashboard', follow_redirects=False)
        
        print(f"  Status: {response.status_code}")
        print("  Headers:")
        for key, value in response.headers:
            print(f"    {key}: {value}")
        
        if response.status_code == 302:
            print(f"  Redirect to: {response.location}")
            print("\n✓ Login endpoint returns redirect (expected)")
        elif response.status_code == 200:
            print(f"  Body preview: {response.data[:200]}")
            print("\n⚠ Login endpoint returned 200 (unexpected - should redirect)")
        else:
            print(f"  Body preview: {response.data[:500]}")
            print(f"\n✗ Login endpoint returned {response.status_code}")
        
except Exception as e:
    print("\n✗ Failed to test auth routes:")
    print(f"  Error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n" + "=" * 80)
print("DIAGNOSTICS COMPLETE")
print("=" * 80)
