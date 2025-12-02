import json
import logging
import requests
import re
import time
import datetime
from flask import Flask, render_template, request, jsonify
from ollama import chat

app = Flask(__name__)

# --- Telemetry Setup ---
# We use a separate logger for telemetry to keep it clean
telemetry_logger = logging.getLogger('telemetry')
telemetry_logger.setLevel(logging.INFO)
telemetry_handler = logging.FileHandler('app_telemetry.jsonl')
telemetry_handler.setFormatter(logging.Formatter('%(message)s'))
telemetry_logger.addHandler(telemetry_handler)

# Standard app logging
logging.basicConfig(level=logging.INFO)

# --- Configuration ---
MODEL_NAME = "qwen2.5:14b"
#MODEL_NAME = "gemma3"
MAX_INPUT_LENGTH = 1000
FORBIDDEN_PHRASES = [
    "ignore previous instructions",
    "forget your rules",
    "system override",
    "delete your system prompt"
]

# --- OPTIMIZED SYSTEM PROMPT ---
SYSTEM_PROMPT_TEXT = """
You are a Magic: The Gathering Commander Deck Building Assistant.
Your goal is to help users build, edit, and refine their 100-card Commander decks.
The user is assumed to be a beginner.

ZERO HALLUCINATION POLICY:
1. YOU MUST NOT INVENT CARD NAMES.
2. Use ONLY real Magic: The Gathering cards.
3. If you are not 100% sure a card exists, DO NOT include it.
4. Fake cards cause the Scryfall API to crash. Be extremely careful with spelling.

CRITICAL RULES:
1. YOU ARE A JSON-ONLY API.
2. DO NOT output any text, markdown, or conversation outside of the JSON object.
3. DO NOT use markdown code blocks (e.g., ```json). Just output the raw JSON string.
4. If you break JSON formatting, the system will fail.
5. DECK SIZE MUST BE EXACTLY 100 CARDS.
   - 1 Commander + 99 Mainboard cards.
   - Do NOT return a partial deck. Pad with basic lands if needed to reach 100.

DATA & SPELLING:

1. Use only your internal knowledge of Magic cards.
2. If "EDHREC Data" is provided in the system context, prioritize those cards for synergy.
3. Ensure card names are spelled EXACTLY right (e.g., "Llanowar Elves", not "Llanowar Elf").

DECK COMPOSITION TEMPLATE (Target Distribution):

- ~12 Ramp
- ~12 Card Advantage
- ~12 Targeted Removal
- ~6 Board Wipes
- ~37 Lands
- ~32 Synergy/Theme Cards

RESPONSE FORMAT (Strict JSON):

{
  "Type": "Deck",
  "Message": "Your helpful response here.",
  "RequestedPrice": 0.00,
  "Theme": "Current Deck Theme (e.g. Artifacts, +1/+1 Counters)",
  "Deck": {
    "Commander": ["Card Name"],
    "Creatures": ["Card Name", ...],
    "Artifacts": ["Card Name", ...],
    "Enchantments": ["Card Name", ...],
    "Instants": ["Card Name", ...],
    "Sorceries": ["Card Name", ...],
    "Planeswalkers": ["Card Name", ...],
    "NonBasicLands": ["Card Name", ...],
    "Lands": ["Card Name", ...]
  }
}
"""

# Global memory
conversation_history = [
    {'role': 'system', 'content': SYSTEM_PROMPT_TEXT}
]

current_deck_state = {}
# Expanded metadata to track Theme
current_deck_meta = {
    "RequestedPrice": 0.00, 
    "CurrentDeckPrice": 0.00,
    "Theme": "None"
}

# EDHREC Cache
edhrec_cache = {}

# --- HELPER: Scryfall Validation & Rescue ---
def validate_cards_with_scryfall(deck_dict):
    """
    Checks cards against Scryfall.
    Layers:
    1. /cards/collection (Bulk check - Fast, Exact)
    2. /cards/search?q= (Search API - Smart, Relevance-based)
    3. Color Identity Check
    """
    # 1. Flatten deck
    all_cards = []
    commander_name = None
    if deck_dict.get("Commander") and len(deck_dict["Commander"]) > 0:
        commander_name = deck_dict["Commander"][0]

    for category in deck_dict:
        all_cards.extend(deck_dict[category])
    
    unique_cards = list(set(all_cards))
    
    if not unique_cards:
        return deck_dict, [], []
    
    # 2. Chunk Requests (Max 75 for Bulk Endpoint)
    chunks = [unique_cards[i:i + 75] for i in range(0, len(unique_cards), 75)]
    valid_map = {}  # Maps 'input_lower' -> 'Official Card Name'
    card_colors = {} # Maps 'Official Card Name' -> color_identity list
    missing_candidates = []
    
    for chunk in chunks:
        identifiers = [{"name": name} for name in chunk]
        try:
            # FIX: Remove markdown formatting from URL
            resp = requests.post("[https://api.scryfall.com/cards/collection](https://api.scryfall.com/cards/collection)", json={"identifiers": identifiers})
            if resp.status_code == 200:
                data = resp.json()
                
                # Successes
                for card in data.get('data', []):
                    valid_map[card['name'].lower()] = card['name']
                    card_colors[card['name']] = card.get('color_identity', [])
                
                # Failures (Candidates for Rescue)
                for missing in data.get('not_found', []):
                    missing_candidates.append(missing['name'])
        except Exception as e:
            logging.error(f"Scryfall Validation Error: {e}")
            # If bulk fails, treat everything as missing so we try to rescue them individually
            missing_candidates.extend(chunk)
    
    # 3. Rescue Logic using Search API
    still_missing = []
    
    for bad_name in missing_candidates:
        # Optimization: Check if we somehow already mapped it
        if bad_name.lower() in valid_map:
            continue
        
        try:
            # Use Scryfall Search API (Powerful fuzzy/relevance matching)
            query = bad_name.replace("'", "")
            # FIX: Remove markdown formatting from URL
            search_resp = requests.get(f"[https://api.scryfall.com/cards/search?q=](https://api.scryfall.com/cards/search?q=\"{query}\")", timeout=1)
            
            if search_resp.status_code == 200:
                search_data = search_resp.json()
                
                if search_data.get('data') and len(search_data['data']) > 0:
                    # Take the top match
                    top_match = search_data['data'][0]
                    real_name = top_match['name']
                    
                    # Map the bad input to the real name
                    valid_map[bad_name.lower()] = real_name
                    card_colors[real_name] = top_match.get('color_identity', [])
                    continue  # Successfully rescued
        except Exception as e:
            logging.error(f"Scryfall Search rescue failed for '{bad_name}': {e}")
        
        # If Search API also failed, mark as truly missing but KEEP IT
        still_missing.append(bad_name)
    
    # 4. Color Identity Check
    illegal_cards = []
    if commander_name:
        # Normalize commander name using valid_map if possible, else original
        cmd_key = commander_name.lower()
        official_cmd = valid_map.get(cmd_key, commander_name)
        
        # If commander was found, get its colors
        if official_cmd in card_colors:
            commander_colors = set(card_colors[official_cmd])
            
            # Check every card in the deck against commander colors
            for c_name, c_colors in card_colors.items():
                # Skip the commander itself
                if c_name == official_cmd: 
                    continue
                
                # Check if card colors are a subset of commander colors
                # c_colors must be subset of commander_colors
                if not set(c_colors).issubset(commander_colors):
                    # Exception: Fetch lands or lands that can produce colorless might have identity rules
                    # But Scryfall 'color_identity' usually handles this correct for Commander.
                    illegal_cards.append(c_name)

    # 5. Reconstruct Deck
    validated_deck = {}
    
    for category, cards in deck_dict.items():
        validated_deck[category] = []
        for card in cards:
            c_lower = card.lower()
            
            # Case 1: Found in Bulk or Rescued via Search
            if c_lower in valid_map:
                validated_deck[category].append(valid_map[c_lower])
            
            # Case 2: Check values for exact casing match
            elif any(c_lower == v.lower() for v in valid_map.values()):
                correct_name = next(v for v in valid_map.values() if c_lower == v.lower())
                validated_deck[category].append(correct_name)
            
            # Case 3: Truly missing -> KEEP ORIGINAL (Do not delete)
            else:
                validated_deck[category].append(card)
    
    return validated_deck, still_missing, illegal_cards

# --- EDHREC Integration ---
def get_edhrec_synergy(commander_list):
    if not commander_list:
        return None
    
    commander_name = commander_list[0]
    
    if commander_name in edhrec_cache:
        return edhrec_cache[commander_name]
    
    try:
        slug = commander_name.lower().replace("'", "").replace(",", "").replace(" // ", "-")
        slug = re.sub(r'[^a-z0-9\s-]', '', slug)
        slug = re.sub(r'\s+', '-', slug)
        
        # FIX: Remove markdown formatting from URL
        url = f"[https://json.edhrec.com/pages/commanders/](https://json.edhrec.com/pages/commanders/){slug}.json"
        response = requests.get(url, timeout=2)
        
        if response.status_code == 200:
            data = response.json()
            recommendations = []
            
            if 'container' in data and 'json_dict' in data['container']:
                cardlists = data['container']['json_dict'].get('cardlists', [])
                
                for section in cardlists:
                    header = section.get('header', '')
                    if header in ["High Synergy Cards", "Top Cards", "Creatures", "Instants", 
                                 "Sorceries", "Utility Artifacts", "Enchantments", "Utility Lands", 
                                 "Mana Artifacts", "Lands"]:
                        for card in section.get('cardviews', [])[:15]:
                            recommendations.append(card['name'])
                
                final_list = list(set(recommendations))[:40]
                edhrec_cache[commander_name] = final_list
                return final_list
    
    except Exception as e:
        logging.error(f"EDHREC Fetch failed: {e}")
    
    return None

def count_deck_cards(deck_dict):
    """Count total cards in deck"""
    total = 0
    for category, cards in deck_dict.items():
        total += len(cards)
    return total

def log_telemetry(pathway, latency, prompt_tokens="N/A"):
    """Logs metrics to jsonl file for assignment requirements"""
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "pathway": pathway,
        "latency_sec": round(latency, 2),
        "model": MODEL_NAME
    }
    telemetry_logger.info(json.dumps(entry))

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/reset', methods=['POST'])
def reset_deck():
    global conversation_history, current_deck_state, current_deck_meta
    
    conversation_history = [
        {'role': 'system', 'content': SYSTEM_PROMPT_TEXT}
    ]
    current_deck_state = {}
    current_deck_meta = {
        "RequestedPrice": 0.00, 
        "CurrentDeckPrice": 0.00,
        "Theme": "None"
    }
    
    return jsonify({"status": "success", "message": "Memory cleared."})

def call_ollama(messages, options=None):
    if options is None:
        options = {"num_ctx": 10240, "temperature": 0.7}
    
    start_time = time.time()
    response = chat(
        model=MODEL_NAME,
        messages=messages,
        stream=False,
        options=options
    )
    duration = time.time() - start_time
    
    # Log the LLM call performance
    log_telemetry("tool_augmented_generation", duration)
    
    content = response['message']['content']
    clean_content = content.strip()
    if clean_content.startswith('```json'):
        clean_content = clean_content.replace('```json', '').replace('```', '')
    return clean_content

@app.route('/chat', methods=['POST'])
def chat_endpoint():
    global conversation_history, current_deck_state, current_deck_meta
    
    data = request.json
    user_input = data.get('message', '')
    client_reported_price = data.get('deckPrice', 0.00)
    
    current_deck_meta["CurrentDeckPrice"] = client_reported_price
    
    # --- SAFETY: Guardrails ---
    # 1. Input Length Check
    if len(user_input) > MAX_INPUT_LENGTH:
        return jsonify({
            "Type": "Deck",
            "Message": f"Error: Message too long. Limit is {MAX_INPUT_LENGTH} characters.",
            "Deck": current_deck_state
        })

    # 2. Prompt Injection Check
    lower_input = user_input.lower()
    for forbidden in FORBIDDEN_PHRASES:
        if forbidden in lower_input:
            log_telemetry("safety_violation", 0)
            return jsonify({
                "Type": "Deck",
                "Message": "I cannot comply with that request. Let's focus on building your deck.",
                "Deck": current_deck_state
            })

    if not user_input:
        return jsonify({"error": "No input provided"}), 400
    
    augmented_input = f"{user_input} Send back a 100 Card Deck!"
    
    conversation_history.append({'role': 'user', 'content': augmented_input})
    
    try:
        # 1. Prepare Initial Context
        messages_to_send = [conversation_history[0]]
        pathway_tools = []
        
        if current_deck_state:
            full_state_context = {
                "Deck": current_deck_state,
                "RequestedPrice": current_deck_meta["RequestedPrice"],
                "CurrentDeckPrice": current_deck_meta["CurrentDeckPrice"],
                "Theme": current_deck_meta["Theme"],
                "CardCount": count_deck_cards(current_deck_state)
            }
            messages_to_send.append({
                'role': 'system',
                'content': f"CURRENT DECK JSON STATE: {json.dumps(full_state_context)}"
            })
        
        commanders = current_deck_state.get("Commander", [])
        if commanders:
            synergy_cards = get_edhrec_synergy(commanders)
            if synergy_cards:
                pathway_tools.append("EDHREC")
                synergy_msg = (
                    f"EDHREC DATA for {commanders[0]}:\n"
                    f"Top Synergy Cards: {', '.join(synergy_cards)}.\n"
                    "Prioritize these cards."
                )
                messages_to_send.append({'role': 'system', 'content': synergy_msg})
        
        recent_messages = conversation_history[1:][-4:]
        messages_to_send.extend(recent_messages)
        
        # 2. First LLM Call
        raw_response = call_ollama(messages_to_send)
        
        try:
            parsed_json = json.loads(raw_response)
        except json.JSONDecodeError as e:
            logging.warning(f"JSON Decode Error: {e}")
            parsed_json = {
                "Type": "Deck",
                "Message": raw_response,
                "Deck": current_deck_state,
                "RequestedPrice": current_deck_meta["RequestedPrice"],
                "Theme": current_deck_meta["Theme"]
            }
        
        # 3. Validation Logic
        validation_message = ""
        
        if parsed_json.get("Deck") and isinstance(parsed_json["Deck"], dict):
            if any(parsed_json["Deck"].values()):
                # A. Validate Names and Color Identity (Tool Use: Scryfall)
                pathway_tools.append("Scryfall_Validation")
                validated_deck, missing_cards, illegal_cards = validate_cards_with_scryfall(parsed_json["Deck"])
                parsed_json["Deck"] = validated_deck
                current_deck_state = validated_deck
                
                if missing_cards:
                    validation_message += f"\n[SYSTEM WARNING]: Missing/Misspelled cards kept: {', '.join(missing_cards[:5])}..."
                
                if illegal_cards:
                    validation_message += f"\n[COLOR IDENTITY WARNING]: These cards are not legal in this commander's color identity: {', '.join(illegal_cards[:5])}..."

                # B. Check Size & Auto-Refine
                card_count = count_deck_cards(validated_deck)
                
                # Logic: If deck is substantial (e.g. > 50) but not 100, ask LLM to finish it
                if 50 < card_count < 100:
                    logging.info(f"Deck incomplete ({card_count}/100). Triggering auto-refinement.")
                    
                    # Create a specific prompt for the refinement
                    needed = 100 - card_count
                    refine_prompt = (
                        f"The deck currently has {card_count} cards. "
                        f"You MUST add exactly {needed} more cards to reach 100. "
                        "Fill the rest with Basic Lands if you run out of ideas. "
                        "Output the COMPLETE updated 100-card deck JSON."
                    )
                    
                    # Add this temporary system instruction to the end of the context
                    refine_messages = messages_to_send + [
                        {'role': 'assistant', 'content': raw_response}, # What it just generated
                        {'role': 'system', 'content': refine_prompt}    # The correction command
                    ]
                    
                    # Call LLM again
                    refined_raw = call_ollama(refine_messages)
                    
                    try:
                        refined_json = json.loads(refined_raw)
                        if refined_json.get("Deck"):
                            # Validate the NEW deck
                            val_refined, miss_refined, illegal_refined = validate_cards_with_scryfall(refined_json["Deck"])
                            new_count = count_deck_cards(val_refined)
                            
                            # Accept the refined deck
                            parsed_json = refined_json
                            parsed_json["Deck"] = val_refined
                            current_deck_state = val_refined
                            parsed_json["CardCount"] = new_count
                            
                            validation_message += f"\n[AUTO-REFINE]: I noticed the deck was short ({card_count} cards), so I added {new_count - card_count} more cards to reach the target."
                            
                            if illegal_refined:
                                 validation_message += f"\n[COLOR IDENTITY WARNING]: These cards are not legal in this commander's color identity: {', '.join(illegal_refined[:5])}..."

                    except json.JSONDecodeError:
                        logging.error("Failed to parse refined deck response.")
                
                parsed_json["CardCount"] = count_deck_cards(current_deck_state)

            else:
                parsed_json["Deck"] = current_deck_state
        else:
            parsed_json["Deck"] = current_deck_state
        
        # 4. Final Metadata Updates
        if "RequestedPrice" in parsed_json:
            current_deck_meta["RequestedPrice"] = parsed_json["RequestedPrice"]
        if "Theme" in parsed_json:
            current_deck_meta["Theme"] = parsed_json["Theme"]
            
        # Append validation messages to the user response
        if validation_message:
            parsed_json["Message"] += ("\n" + validation_message)
        
        # Save only the message to history to save tokens
        history_entry = {
            "Type": "Deck",
            "Message": parsed_json.get("Message", "")
        }
        conversation_history.append({'role': 'assistant', 'content': json.dumps(history_entry)})
        
        return jsonify(parsed_json)
    
    except Exception as e:
        logging.error(f"Error: {e}", exc_info=True)
        return jsonify({
            "Type": "Deck",
            "Message": f"Error: {str(e)}",
            "Deck": current_deck_state,
            "CardCount": count_deck_cards(current_deck_state)
        })

if __name__ == '__main__':
    app.run(debug=True, port=5001)