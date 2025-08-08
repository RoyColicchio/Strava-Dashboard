import streamlit as st
st.set_page_config(layout="wide")
from streamlit_oauth import OAuth2Component
import json
from pathlib import Path
from st_aggrid import AgGrid, GridOptionsBuilder, JsCode
from pace_utils import marathon_pace_seconds, get_pace_range
import requests
import time
import os
import pandas as pd
import re
from datetime import datetime, timedelta
import os

# Resolve Google OAuth credentials from Streamlit Secrets or env vars
google_client_id = st.secrets.get("google_client_id") or os.getenv("GOOGLE_CLIENT_ID")
google_client_secret = st.secrets.get("google_client_secret") or os.getenv("GOOGLE_CLIENT_SECRET")
if not google_client_id or not google_client_secret:
    st.error("Missing Google OAuth credentials. Set google_client_id and google_client_secret in Streamlit Cloud Secrets or .streamlit/secrets.toml (or env vars GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).")
    st.stop()

# --- Google OAuth2 Authentication ---
oauth2 = OAuth2Component(
    client_id=google_client_id,
    client_secret=google_client_secret,
    authorize_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
    token_endpoint="https://oauth2.googleapis.com/token",
    refresh_token_endpoint="https://oauth2.googleapis.com/token",
    revoke_token_endpoint="https://oauth2.googleapis.com/revoke"
)

# Check if user is already logged in
if "user_email" not in st.session_state:
    try:
        result = oauth2.authorize_button(
            "Login with Google", 
            key="google_auth",
            redirect_uri="https://marathonplanner.streamlit.app/",
            scope="openid email profile"
        )
        if result and "token" in result:
            # Get user info directly from Google's userinfo endpoint
            headers = {"Authorization": f"Bearer {result['token']['access_token']}"}
            response = requests.get("https://openidconnect.googleapis.com/v1/userinfo", headers=headers)
            if response.status_code == 200:
                user_info = response.json()
                st.session_state["user_email"] = user_info["email"]
                st.session_state["user_name"] = user_info.get("given_name", user_info.get("name", "User"))
                st.success(f"Logged in as {st.session_state['user_name']}")
                st.rerun()
            else:
                st.error("Failed to get user information from Google")
                st.stop()
        else:
            st.info("Please click 'Login with Google' to continue")
            st.stop()
    except Exception as e:
        st.error("Authentication error occurred. Please try refreshing the page.")
        # Clear any stale session state
        if "user_email" in st.session_state:
            del st.session_state["user_email"]
        st.stop()
else:
    st.success(f"Welcome back, {st.session_state.get('user_name', 'User')}!")

def setup_screen():
    """Show setup screen for new users or those who haven't completed setup"""
    st.title("🏃‍♂️ Marathon Planner Setup")
    
    user_name = st.session_state.get("user_name", "User")
    st.write(f"Hi {user_name}! Let's get you set up with your training plan.")
    
    with st.form("setup_form"):
        st.subheader("Training Plan Setup")
        
        # Plan selection dropdown with friendly name
        plan_options = {"run_plan.csv": "Pfitz 18/55"}
        plan_labels = list(plan_options.values())
        plan_label = st.selectbox("Select your training plan", plan_labels, index=0)
        selected_plan_file = [k for k, v in plan_options.items() if v == plan_label][0]
        
        # Start date selection
        start_date_input = st.date_input("Select your plan start date")
        
        # Goal time input
        goal_time_input = st.text_input("Enter your marathon goal time (hh:mm:ss)", value="3:30:00", placeholder="3:30:00")
        
        submitted = st.form_submit_button("Connect Strava & Start Training Plan", use_container_width=True)
        
        if submitted:
            # Validate inputs
            if not start_date_input:
                st.error("Please select a start date")
                return False
            if not goal_time_input or goal_time_input.strip() == "":
                st.error("Please enter a goal time")
                return False
            
            # Save user settings first
            user_email = st.session_state["user_email"]
            user_name = st.session_state.get("user_name", "User")
            
            settings_path = Path("user_settings.json")
            if settings_path.exists():
                with open(settings_path, "r") as f:
                    all_settings = json.load(f)
            else:
                all_settings = {}
            
            user_settings = {
                "name": user_name,
                "start_date": str(start_date_input),
                "plan": selected_plan_file,
                "goal_time": goal_time_input.strip(),
                "strava_connected": False  # Will be set to True after Strava OAuth
            }
            
            all_settings[user_email] = user_settings
            with open(settings_path, "w") as f:
                json.dump(all_settings, f, indent=2)
            
            # Redirect to Strava OAuth
            st.session_state["setup_complete"] = True
            st.session_state["need_strava_auth"] = True
            st.success("Settings saved! Now let's connect your Strava account...")
            st.rerun()
    
    return False

def strava_oauth_screen():
    """Show Strava OAuth screen"""
    st.title("🚴‍♂️ Connect Your Strava Account")
    
    user_name = st.session_state.get("user_name", "User")
    st.write(f"Hi {user_name}! To see your actual running data, we need to connect your Strava account.")
    
    st.info("We'll only access your activity data (runs, distance, pace) to compare with your training plan.")
    
    # Check for errors first
    query_params = st.query_params
    if "error" in query_params:
        error_msg = query_params.get("error", "Unknown error")
        st.error(f"Strava OAuth error: {error_msg}")
        if error_msg == "access_denied":
            st.write("You denied access to your Strava account. You can try connecting again or use demo mode.")
        st.markdown("---")
    
    # Strava OAuth parameters
    strava_client_id = "171563"  # Your Strava app client ID
    redirect_uri = "https://marathonplanner.streamlit.app"  # Remove trailing slash
    scope = "read,activity:read_all"
    
    # Add a unique state to prevent CSRF and help with debugging
    import hashlib
    import time
    state_data = f"{st.session_state['user_email']}_{int(time.time())}"
    state = hashlib.md5(state_data.encode()).hexdigest()[:16]
    
    # Generate Strava OAuth URL
    strava_auth_url = f"https://www.strava.com/oauth/authorize?client_id={strava_client_id}&redirect_uri={redirect_uri}&response_type=code&scope={scope}&state={state}&approval_prompt=force"
    
    # Check if we have too many attempts (to avoid the challenge error)
    attempt_count = st.session_state.get("strava_attempts", 0)
    last_attempt_time = st.session_state.get("last_strava_attempt", 0)
    current_time = time.time()
    
    # Reset attempts if enough time has passed (15 minutes)
    if current_time - last_attempt_time > 900:  # 15 minutes
        st.session_state["strava_attempts"] = 0
        attempt_count = 0
    
    if attempt_count >= 2:  # Reduced threshold to be more conservative
        remaining_time = 900 - (current_time - last_attempt_time)
        minutes_left = int(remaining_time / 60)
        
        st.error("🚫 Strava Rate Limit Reached")
        st.write(f"Too many connection attempts detected. Strava has temporarily blocked OAuth requests.")
        st.write(f"**Wait time:** {minutes_left} minutes remaining")
        st.info("💡 **Recommendation:** Use Demo Mode to explore the app while waiting, or try connecting later.")
        
        # Show a progress bar for the wait time
        if remaining_time > 0:
            progress = (900 - remaining_time) / 900
            st.progress(progress)
            st.write(f"Rate limit will reset in {minutes_left} minutes")
    else:
        st.write("**Debug Info:**")
        st.write(f"Redirect URI: `{redirect_uri}`")
        st.write(f"Client ID: `{strava_client_id}`")
        st.write(f"State: `{state}`")
        st.write(f"Attempts: {attempt_count}/3")
        
        # Check current URL params
        if query_params:
            st.write(f"**Current URL params:** {dict(query_params)}")
        
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            if st.button("🚴‍♂️ Connect Strava Account", type="primary", use_container_width=True):
                # Record attempt time and increment counter
                st.session_state["strava_attempts"] = attempt_count + 1
                st.session_state["last_strava_attempt"] = current_time
                
                # Use a more direct redirect approach
                st.write("🔄 Redirecting to Strava...")
                st.markdown(f'<meta http-equiv="refresh" content="2; url={strava_auth_url}">', unsafe_allow_html=True)
                
                # Add a manual fallback
                st.write("If you're not redirected automatically, [click here to connect Strava]({})".format(strava_auth_url))
        
        # Add manual link as fallback
        st.markdown("---")
        st.write("**Alternative**: Copy and paste this URL in a **new private/incognito window**:")
        st.code(strava_auth_url, language=None)
        st.write("💡 Using a private window can help avoid rate limit issues.")
    
    # Check for authorization code in URL params
    if "code" in query_params and attempt_count < 2:  # Updated to match new threshold
        auth_code = query_params["code"]
        received_state = query_params.get("state", "")
        
        st.write("✅ Authorization received! Processing...")
        st.write(f"Code: {auth_code[:10]}...")
        st.write(f"State: {received_state}")
        
        # Exchange code for access token
        token_url = "https://www.strava.com/api/v3/oauth/token"
        data = {
            "client_id": strava_client_id,
            "client_secret": "db5b605f66158bcf80d1ddda5a6a2739e66899dd",  # Your Strava app secret
            "code": auth_code,
            "grant_type": "authorization_code"
        }
        
        try:
            response = requests.post(token_url, data=data, timeout=10)
            if response.status_code == 200:
                token_data = response.json()
                
                # Save user's Strava tokens
                user_email = st.session_state["user_email"]
                settings_path = Path("user_settings.json")
                
                with open(settings_path, "r") as f:
                    all_settings = json.load(f)
                
                user_settings = all_settings.get(user_email, {})
                user_settings["strava_access_token"] = token_data["access_token"]
                user_settings["strava_refresh_token"] = token_data["refresh_token"]
                user_settings["strava_expires_at"] = token_data["expires_at"]
                user_settings["strava_connected"] = True
                
                all_settings[user_email] = user_settings
                with open(settings_path, "w") as f:
                    json.dump(all_settings, f, indent=2)
                
                st.success("🎉 Strava connected successfully!")
                st.session_state["strava_connected"] = True
                if "need_strava_auth" in st.session_state:
                    del st.session_state["need_strava_auth"]
                # Clear attempt counter on success
                if "strava_attempts" in st.session_state:
                    del st.session_state["strava_attempts"]
                
                # Clear query params and redirect
                st.query_params.clear()
                time.sleep(1)  # Brief pause before redirect
                st.rerun()
            else:
                st.error(f"Failed to connect Strava: HTTP {response.status_code}")
                st.write(f"Response: {response.text}")
        except Exception as e:
            st.error(f"Error connecting to Strava: {str(e)}")
    
    # Option to skip Strava connection
    st.markdown("---")
    st.write("### Alternative Options:")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("📊 Skip Strava Connection (Demo Mode)", use_container_width=True):
            # Mark as skipped but allow dashboard access
            user_email = st.session_state["user_email"]
            settings_path = Path("user_settings.json")
            
            with open(settings_path, "r") as f:
                all_settings = json.load(f)
            
            user_settings = all_settings.get(user_email, {})
            user_settings["strava_connected"] = False
            user_settings["demo_mode"] = True
            
            all_settings[user_email] = user_settings
            with open(settings_path, "w") as f:
                json.dump(all_settings, f, indent=2)
            
            if "need_strava_auth" in st.session_state:
                del st.session_state["need_strava_auth"]
            # Clear attempt counter
            if "strava_attempts" in st.session_state:
                del st.session_state["strava_attempts"]
            if "last_strava_attempt" in st.session_state:
                del st.session_state["last_strava_attempt"]
            st.success("🎉 Demo mode activated! Redirecting to dashboard...")
            time.sleep(1)
            st.rerun()
    
    with col2:
        if st.button("🔄 Clear Rate Limit & Try Again", use_container_width=True):
            # Force clear attempt counter and timestamps
            if "strava_attempts" in st.session_state:
                del st.session_state["strava_attempts"]
            if "last_strava_attempt" in st.session_state:
                del st.session_state["last_strava_attempt"]
            st.query_params.clear()
            st.success("Rate limit cleared! You can try connecting again.")
            time.sleep(2)
            st.rerun()

def dashboard_logic(name, username):
    st.title("🏃‍♂️ Marathon Training Dashboard")
    st.write(f"Welcome, {name}!")
    
    # Load user settings (they should exist since setup is complete)
    settings_path = Path("user_settings.json")
    if settings_path.exists():
        with open(settings_path, "r") as f:
            all_settings = json.load(f)
    else:
        st.error("Setup data not found. Please refresh the page.")
        st.stop()
    
    user_settings = all_settings.get(username, {})
    if not user_settings:
        st.error("User settings not found. Please refresh the page.")
        st.stop()
    
    # Get user settings
    plan_choice = user_settings["plan"]
    start_date = user_settings["start_date"]
    goal_marathon_time = user_settings["goal_time"]
    
    # Show current settings in sidebar
    with st.sidebar:
        st.header("Your Settings")
        st.write(f"**Plan:** Pfitz 18/55")
        st.write(f"**Start Date:** {start_date}")
        st.write(f"**Goal Time:** {goal_marathon_time}")
        if st.button("Update Settings"):
            # Clear setup completion to show setup screen again
            if "setup_complete" in st.session_state:
                del st.session_state["setup_complete"]
            st.rerun()
    # If start_date is a string, parse it to datetime.date
    if isinstance(start_date, str) and start_date not in ("", "NaT", None):
        try:
            start_date_parsed = datetime.strptime(start_date, "%Y-%m-%d").date()
            start_date = start_date_parsed
        except Exception as e:
            pass
    # Guard: start_date must be valid
    if not start_date or start_date in ["", None, "NaT"]:
        st.error("No valid start date set. Please select a start date in the sidebar.")
        st.stop()
    try:
        start = pd.to_datetime(start_date)
    except Exception as e:
        st.error(f"Invalid start date: {start_date}. Error: {e}")
        st.stop()
    try:
        activities = get_activities(username)
        comparison = compare_plan_vs_actual(activities, plan_choice, start_date)
        # Add suggested pace column using goal_marathon_time and activity type, and move it to the left of 'Planned Distance (mi)'
        # Convert goal_marathon_time to seconds once
        try:
            gmp_sec = marathon_pace_seconds(goal_marathon_time)
        except Exception as e:
            st.write(f"Error parsing goal marathon time '{goal_marathon_time}': {e}")
            gmp_sec = None

        def get_suggested_pace(row):
            try:
                if gmp_sec is None:
                    return ""
                return get_pace_range(row['Activity'], gmp_sec)
            except Exception:
                return ""
        # Insert 'Suggested Pace' to the left of 'Planned Distance (mi)'
        insert_idx = comparison.columns.get_loc('Planned Distance (mi)')
        comparison.insert(insert_idx, 'Suggested Pace', comparison.apply(get_suggested_pace, axis=1))
        st.subheader("📅 Plan vs. Actual")
        columns_to_hide = ["Calendar Date", "Calendar Date Str", "Hit?"]
        display_df = comparison.drop(columns=[col for col in columns_to_hide if col in comparison.columns])
        # Shrink the width of the rightmost 4 columns (Planned Distance, Actual Distance, Diff, Suggested Pace)
        gb = GridOptionsBuilder.from_dataframe(display_df)
        # Set default min/max width for all columns
        gb.configure_default_column(minWidth=80, maxWidth=300)
        # Find the rightmost 4 columns
        rightmost_cols = display_df.columns[-4:]
        for col in rightmost_cols:
            gb.configure_column(col, minWidth=70, maxWidth=120, width=90)
        grid_options = gb.build()
        AgGrid(display_df, gridOptions=grid_options, theme="streamlit", fit_columns_on_grid_load=True)
        rec, expl = make_recommendation(activities, plan_choice, start_date)
        st.subheader("💡 Recommendation")
        st.write(rec)
        with st.expander("Show details"):
            st.text(expl)
        # Weekly mileage section removed as requested
    except Exception as e:
        st.error(f"Error showing plan: {e}")

# --- Per-user Strava functions ---
def get_user_strava_token(user_email):
    """Get user's Strava access token, refreshing if necessary"""
    settings_path = Path("user_settings.json")
    if not settings_path.exists():
        return None
    
    with open(settings_path, "r") as f:
        all_settings = json.load(f)
    
    user_settings = all_settings.get(user_email, {})
    if not user_settings.get("strava_connected"):
        return None
    
    access_token = user_settings.get("strava_access_token")
    expires_at = user_settings.get("strava_expires_at", 0)
    
    # Check if token is expired
    if time.time() > expires_at:
        # Refresh the token
        refresh_token = user_settings.get("strava_refresh_token")
        if not refresh_token:
            return None
        
        response = requests.post(
            url="https://www.strava.com/api/v3/oauth/token",
            data={
                "client_id": "171563",
                "client_secret": "db5b605f66158bcf80d1ddda5a6a2739e66899dd",
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        
        if response.status_code == 200:
            new_tokens = response.json()
            
            # Update user settings with new tokens
            user_settings["strava_access_token"] = new_tokens["access_token"]
            user_settings["strava_refresh_token"] = new_tokens["refresh_token"]
            user_settings["strava_expires_at"] = new_tokens["expires_at"]
            
            all_settings[user_email] = user_settings
            with open(settings_path, "w") as f:
                json.dump(all_settings, f, indent=2)
            
            return new_tokens["access_token"]
        else:
            return None
    
    return access_token

def get_activities(user_email):
    """Get activities for specific user"""
    settings_path = Path("user_settings.json")
    with open(settings_path, "r") as f:
        all_settings = json.load(f)
    
    user_settings = all_settings.get(user_email, {})
    
    # If in demo mode, return sample data
    if user_settings.get("demo_mode"):
        return []  # Empty activities for demo mode
    
    access_token = get_user_strava_token(user_email)
    if not access_token:
        st.error("Strava connection lost. Please reconnect your Strava account.")
        return []
    
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get("https://www.strava.com/api/v3/athlete/activities", headers=headers)
    
    if r.status_code == 200:
        return r.json()
    else:
        st.error(f"Failed to fetch Strava activities: {r.status_code}")
        return []

def display_weekly_mileage(activities):
    df = pd.DataFrame(activities)
    df['start_date'] = pd.to_datetime(df['start_date'])
    df['week'] = df['start_date'].dt.isocalendar().week
    df['year'] = df['start_date'].dt.year
    df['distance_mi'] = df['distance'] / 1609.34  # meters to miles

    weekly_miles = df.groupby(['year', 'week'])['distance_mi'].sum().reset_index()
    weekly_miles = weekly_miles.sort_values(['year', 'week'], ascending=False)

    st.subheader("📈 Weekly Mileage")
    st.dataframe(weekly_miles, use_container_width=True)

def load_run_plan(plan_path, start_date):
    # Debug: print the first 7 computed dates and days
    debug_rows = []
    for i in range(min(7, len(plan_df))):
        debug_rows.append((plan_df['Calendar Date'].iloc[i], plan_df['Day'].iloc[i]))
    print("DEBUG: First 7 Calendar Dates and Days:")
    for d, day in debug_rows:
        print(f"  {d} => {day}")
    plan_df = pd.read_csv(plan_path)
    plan_df.columns = plan_df.columns.str.strip()
    if 'Date' not in plan_df.columns:
        st.write('Plan CSV columns:', plan_df.columns.tolist())
        raise KeyError("'Date' column not found in run_plan.csv. Check the CSV header row.")
    import re
    plan_df = plan_df[plan_df['Date'].notnull() & plan_df['Plan'].notnull()]
    plan_df['Date'] = plan_df['Date'].astype(str)
    def extract_miles(plan):
        match = re.search(r'(\d+(?:\.\d+)?)', str(plan))
        return float(match.group(1)) if match else 0.0
    plan_df['Planned Distance (mi)'] = plan_df['Plan'].apply(extract_miles)
    plan_df = plan_df.reset_index(drop=True)
    # Guard: start_date must be valid
    if not start_date or start_date in ["", None, "NaT"]:
        st.error("No valid start date set. Please select a start date in the sidebar.")
        st.stop()
    try:
        start = pd.to_datetime(start_date)
    except Exception as e:
        st.error(f"Invalid start date: {start_date}. Error: {e}")
        st.stop()
    plan_dates = [start + timedelta(days=i) for i in range(len(plan_df))]
    plan_df['Calendar Date'] = [pd.to_datetime(d).date() for d in plan_dates]
    plan_df['Calendar Date Str'] = plan_df['Calendar Date'].astype(str)
    # Remove any existing Day column from CSV
    if 'Day' in plan_df.columns:
        plan_df = plan_df.drop(columns=['Day'])
    # Always compute the Day column from the Calendar Date
    weekday_map = {0: 'M', 1: 'Tu', 2: 'W', 3: 'Th', 4: 'F', 5: 'Sa', 6: 'Su'}
    plan_df['Day'] = [weekday_map[d.weekday()] for d in plan_df['Calendar Date']]
    def expand_activity(plan):
        mapping = {
            'GA': 'General Aerobic',
            'Sp': 'Sprints',
            'MP': 'Marathon Pace',
            'LT': 'Lactate Threshold',
            'HMP': 'Half Marathon Pace',
            'Rec': 'Recovery',
            'MLR': 'Medium-Long Run',
            'LR': 'Long Run'
        }
        s = str(plan)
        for abbr, full in mapping.items():
            s = s.replace(abbr, full)
        return s
    plan_df['Activity'] = plan_df['Plan'].apply(expand_activity)
    return plan_df[['Calendar Date', 'Calendar Date Str', 'Date', 'Day', 'Activity', 'Planned Distance (mi)']]

def compare_plan_vs_actual(activities, plan_path, start_date):
    plan = load_run_plan(plan_path, start_date)
    df = pd.DataFrame(activities)
    df['start_date'] = pd.to_datetime(df['start_date'])
    df['date_str'] = df['start_date'].dt.strftime('%Y-%m-%d')
    df['distance_mi'] = df['distance'] / 1609.34

    # Sum actual miles per day (in case of multiple runs)
    actual = df.groupby('date_str')['distance_mi'].sum().reset_index()
    actual = actual.rename(columns={'distance_mi': 'Actual Distance (mi)', 'date_str': 'Calendar Date Str'})

    merged = plan.merge(actual, on='Calendar Date Str', how='left')
    merged['Actual Distance (mi)'] = merged['Actual Distance (mi)'].fillna(0)
    merged['Planned Distance (mi)'] = merged['Planned Distance (mi)'].fillna(0)
    today = pd.Timestamp.today().date()
    def diff_if_past(row):
        if row['Calendar Date'] < today:
            return row['Actual Distance (mi)'] - row['Planned Distance (mi)']
        return ""
    def hit_if_past(row):
        if row['Calendar Date'] < today:
            planned = row['Planned Distance (mi)']
            actual = row['Actual Distance (mi)']
            if planned == 0 and actual == 0:
                return True
            if planned == 0:
                return False
            return abs(actual - planned) / planned <= 0.2
        return ""
    def actual_if_past(row):
        if row['Calendar Date'] <= today:
            val = row['Actual Distance (mi)']
            # Show blank if value is 0 and in the future
            if pd.isna(val) or (row['Calendar Date'] > today):
                return ""
            # Show blank if value is 0 and not in the past
            if row['Calendar Date'] == today and val == 0:
                return ""
            return str(val) if val != 0 else ""
        return ""
    merged['Diff (mi)'] = merged.apply(diff_if_past, axis=1)
    merged['Hit?'] = merged.apply(hit_if_past, axis=1)
    merged['Actual Distance (mi)'] = merged.apply(actual_if_past, axis=1).astype(str)
    # Sort so today is at the top, past above, future below
    merged['sort_key'] = (merged['Calendar Date'] - today).apply(lambda x: x.days)
    merged = merged.sort_values('sort_key', key=lambda x: x.abs())
    merged = merged.reset_index(drop=True)
    merged['Calendar MM/DD'] = pd.to_datetime(merged['Calendar Date']).dt.strftime('%m/%d')
    return merged[['Calendar Date', 'Calendar Date Str', 'Calendar MM/DD', 'Day', 'Activity', 'Planned Distance (mi)', 'Actual Distance (mi)', 'Diff (mi)', 'Hit?']]

def make_recommendation(activities, plan_path, start_date):
    plan = load_run_plan(plan_path, start_date)
    df = pd.DataFrame(activities)
    df['start_date'] = pd.to_datetime(df['start_date'])
    df['date_str'] = df['start_date'].dt.strftime('%Y-%m-%d')
    df['distance_mi'] = df['distance'] / 1609.34

    today = pd.Timestamp.today().normalize()
    last_7 = [today - pd.Timedelta(days=i) for i in range(7)]
    last_7_str = [d.strftime('%Y-%m-%d') for d in last_7]

    plan7 = plan[plan['Calendar Date Str'].isin(last_7_str)].set_index('Calendar Date Str')
    actual7 = df.groupby('date_str')['distance_mi'].sum()

    summary = []
    for d in last_7_str:
        planned = plan7['Planned Distance (mi)'].get(d, 0)
        actual = actual7.get(d, 0)
        summary.append({'date': d, 'planned': planned, 'actual': actual, 'diff': actual - planned})

    last2 = summary[1:3]
    last2_diff = sum(x['diff'] for x in last2)
    today_plan = summary[0]['planned']

    rec = ""
    if last2_diff < -1:
        rec = f"You've been under your plan by {abs(last2_diff):.1f} miles the last two days. Consider adding 1 mile to today's planned run (planned: {today_plan:.1f} mi → recommended: {today_plan+1:.1f} mi)."
    elif last2_diff > 1:
        rec = f"You've been over your plan by {last2_diff:.1f} miles the last two days. Consider going 1 mile shorter today (planned: {today_plan:.1f} mi → recommended: {max(today_plan-1,0):.1f} mi)."
    else:
        rec = f"You're close to your plan for the last two days. Stick with today's planned run: {today_plan:.1f} mi."

    expl = (
        f"Last 7 days (most recent first):\n" +
        "\n".join([f"{x['date']}: planned {x['planned']:.1f}, actual {x['actual']:.1f}, diff {x['diff']:+.1f}" for x in summary])
    )
    return rec, expl

# --- Guest mode persistence ---


def parse_time_to_seconds(timestr):
    parts = [int(p) for p in timestr.strip().split(":")]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m = 0, parts[0]
        s = parts[1]
    else:
        raise ValueError("Invalid time format")
    return h * 3600 + m * 60 + s
import requests
import json
import time
import pandas as pd

import numpy as np
from datetime import datetime, timedelta


# Display weekly mileage
def display_weekly_mileage(activities):
    df = pd.DataFrame(activities)
    df['start_date'] = pd.to_datetime(df['start_date'])
    df['week'] = df['start_date'].dt.isocalendar().week
    df['year'] = df['start_date'].dt.year
    df['distance_mi'] = df['distance'] / 1609.34  # meters to miles

    weekly_miles = df.groupby(['year', 'week'])['distance_mi'].sum().reset_index()
    weekly_miles = weekly_miles.sort_values(['year', 'week'], ascending=False)

    st.subheader("📈 Weekly Mileage")
    st.dataframe(weekly_miles, use_container_width=True)

# Load run plan from CSV
def load_run_plan(plan_path, start_date):
    plan_df = pd.read_csv(plan_path)
    plan_df.columns = plan_df.columns.str.strip()
    if 'Date' not in plan_df.columns:
        st.write('Plan CSV columns:', plan_df.columns.tolist())
        raise KeyError("'Date' column not found in run_plan.csv. Check the CSV header row.")
    import re
    plan_df = plan_df[plan_df['Date'].notnull() & plan_df['Plan'].notnull()]
    plan_df['Date'] = plan_df['Date'].astype(str)
    def extract_miles(plan):
        match = re.search(r'(\d+(?:\.\d+)?)', str(plan))
        return float(match.group(1)) if match else 0.0
    plan_df['Planned Distance (mi)'] = plan_df['Plan'].apply(extract_miles)
    # Assign actual calendar dates based on user-selected start date
    plan_df = plan_df.reset_index(drop=True)
    # Guard: start_date must be valid
    if not start_date or start_date in ["", None, "NaT"]:
        st.error("No valid start date set. Please select a start date in the sidebar.")
        st.stop()
    try:
        start = pd.to_datetime(start_date)
    except Exception as e:
        st.error(f"Invalid start date: {start_date}. Error: {e}")
        st.stop()
    plan_dates = []
    for i, row in plan_df.iterrows():
        plan_dates.append(start + timedelta(days=i))
    plan_df['Calendar Date'] = [pd.to_datetime(d).date() for d in plan_dates]
    plan_df['Calendar Date Str'] = plan_df['Calendar Date'].astype(str)
    # Remove any existing Day column from CSV
    if 'Day' in plan_df.columns:
        plan_df = plan_df.drop(columns=['Day'])
    # Always compute the Day column from the Calendar Date
    weekday_map = {0: 'M', 1: 'Tu', 2: 'W', 3: 'Th', 4: 'F', 5: 'Sa', 6: 'Su'}
    plan_df['Day'] = [weekday_map[d.weekday()] for d in plan_df['Calendar Date']]
    # Debug: print the first 7 computed dates and days
    debug_rows = []
    for i in range(min(7, len(plan_df))):
        debug_rows.append((plan_df['Calendar Date'].iloc[i], plan_df['Day'].iloc[i]))
    print("DEBUG: First 7 Calendar Dates and Days:")
    for d, day in debug_rows:
        print(f"  {d} => {day}")
    def expand_activity(plan):
        mapping = {
            'GA': 'General Aerobic',
            'Sp': 'Sprints',
            'MP': 'Marathon Pace',
            'LT': 'Lactate Threshold',
            'HMP': 'Half Marathon Pace',
            'Rec': 'Recovery',
            'MLR': 'Medium-Long Run',
            'LR': 'Long Run'
        }
        s = str(plan)
        for abbr, full in mapping.items():
            s = s.replace(abbr, full)
        return s
    plan_df['Activity'] = plan_df['Plan'].apply(expand_activity)
    return plan_df[['Calendar Date', 'Calendar Date Str', 'Date', 'Day', 'Activity', 'Planned Distance (mi)']]

# Compare plan vs. actual
def compare_plan_vs_actual(activities, plan_path, start_date):
    plan = load_run_plan(plan_path, start_date)
    df = pd.DataFrame(activities)
    df['start_date'] = pd.to_datetime(df['start_date'])
    df['date_str'] = df['start_date'].dt.strftime('%Y-%m-%d')
    df['distance_mi'] = df['distance'] / 1609.34

    # Sum actual miles per day (in case of multiple runs)
    actual = df.groupby('date_str')['distance_mi'].sum().reset_index()
    actual = actual.rename(columns={'distance_mi': 'Actual Distance (mi)', 'date_str': 'Calendar Date Str'})

    merged = plan.merge(actual, on='Calendar Date Str', how='left')
    merged['Actual Distance (mi)'] = merged['Actual Distance (mi)'].fillna(0)
    merged['Planned Distance (mi)'] = merged['Planned Distance (mi)'].fillna(0)
    today = pd.Timestamp.today().date()
    def diff_if_past(row):
        if row['Calendar Date'] < today:
            return row['Actual Distance (mi)'] - row['Planned Distance (mi)']
        return ""
    def hit_if_past(row):
        if row['Calendar Date'] < today:
            planned = row['Planned Distance (mi)']
            actual = row['Actual Distance (mi)']
            if planned == 0 and actual == 0:
                return True
            if planned == 0:
                return False
            return abs(actual - planned) / planned <= 0.2
        return ""
    def actual_if_past(row):
        if row['Calendar Date'] <= today:
            val = row['Actual Distance (mi)']
            # Show blank if value is 0 and in the future
            if pd.isna(val) or (row['Calendar Date'] > today):
                return ""
            # Show blank if value is 0 and not in the past
            if row['Calendar Date'] == today and val == 0:
                return ""
            return str(val) if val != 0 else ""
        return ""
    merged['Diff (mi)'] = merged.apply(diff_if_past, axis=1)
    merged['Hit?'] = merged.apply(hit_if_past, axis=1)
    merged['Actual Distance (mi)'] = merged.apply(actual_if_past, axis=1).astype(str)
    # Sort so today is at the top, past above, future below
    merged['sort_key'] = (merged['Calendar Date'] - today).apply(lambda x: x.days)
    merged = merged.sort_values('sort_key', key=lambda x: x.abs())
    merged = merged.reset_index(drop=True)
    merged['Calendar MM/DD'] = pd.to_datetime(merged['Calendar Date']).dt.strftime('%m/%d')
    return merged[['Calendar Date', 'Calendar Date Str', 'Calendar MM/DD', 'Day', 'Activity', 'Planned Distance (mi)', 'Actual Distance (mi)', 'Diff (mi)', 'Hit?']]

# Recommendation section
def make_recommendation(activities, plan_path, start_date):
    plan = load_run_plan(plan_path, start_date)
    df = pd.DataFrame(activities)
    df['start_date'] = pd.to_datetime(df['start_date'])
    df['date_str'] = df['start_date'].dt.strftime('%Y-%m-%d')
    df['distance_mi'] = df['distance'] / 1609.34

    today = pd.Timestamp.today().normalize()
    last_7 = [today - pd.Timedelta(days=i) for i in range(7)]
    last_7_str = [d.strftime('%Y-%m-%d') for d in last_7]

    plan7 = plan[plan['Calendar Date Str'].isin(last_7_str)].set_index('Calendar Date Str')
    actual7 = df.groupby('date_str')['distance_mi'].sum()

    summary = []
    for d in last_7_str:
        planned = plan7['Planned Distance (mi)'].get(d, 0)
        actual = actual7.get(d, 0)
        summary.append({'date': d, 'planned': planned, 'actual': actual, 'diff': actual - planned})

    last2 = summary[1:3]
    last2_diff = sum(x['diff'] for x in last2)
    today_plan = summary[0]['planned']

    rec = ""
    if last2_diff < -1:
        rec = f"You've been under your plan by {abs(last2_diff):.1f} miles the last two days. Consider adding 1 mile to today's planned run (planned: {today_plan:.1f} mi → recommended: {today_plan+1:.1f} mi)."
    elif last2_diff > 1:
        rec = f"You've been over your plan by {last2_diff:.1f} miles the last two days. Consider going 1 mile shorter today (planned: {today_plan:.1f} mi → recommended: {max(today_plan-1,0):.1f} mi)."
    else:
        rec = f"You're close to your plan for the last two days. Stick with today's planned run: {today_plan:.1f} mi."

    expl = (
        f"Last 7 days (most recent first):\n" +
        "\n".join([f"{x['date']}: planned {x['planned']:.1f}, actual {x['actual']:.1f}, diff {x['diff']:+.1f}" for x in summary])
    )
    return rec, expl


# Streamlit UI
login_placeholder = st.empty()

# Use Google OAuth user info for dashboard
user_email = st.session_state.get("user_email")
user_name = st.session_state.get("user_name", "User")

if user_email:
    # Check if user has completed setup
    settings_path = Path("user_settings.json")
    setup_complete = False
    strava_connected = False
    
    if settings_path.exists():
        with open(settings_path, "r") as f:
            all_settings = json.load(f)
        user_settings = all_settings.get(user_email, {})
        # Check if all required settings exist
        if (user_settings.get("start_date") and 
            user_settings.get("plan") and 
            user_settings.get("goal_time")):
            setup_complete = True
        # Check if Strava is connected
        strava_connected = user_settings.get("strava_connected", False) or user_settings.get("demo_mode", False)
    
    # Override with session state if setup was just completed
    if st.session_state.get("setup_complete"):
        setup_complete = True
    
    if not setup_complete:
        setup_screen()
    elif st.session_state.get("need_strava_auth") or (setup_complete and not strava_connected):
        strava_oauth_screen()
    else:
        dashboard_logic(user_name, user_email)





