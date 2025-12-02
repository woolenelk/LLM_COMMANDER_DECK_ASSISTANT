import json
import logging
import requests
import re
import time
import datetime
import os
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# --- Telemetry Setup ---
telemetry_logger = logging.getLogger('telemetry')
telemetry_logger.setLevel(logging.INFO)
telemetry_handler = logging.FileHandler('app_telemetry.jsonl')
telemetry_handler.setFormatter(logging.Formatter('%(message)s'))
telemetry_logger.addHandler(telemetry_handler)

logging.basicConfig(level=logging.INFO)

# --- Configuration ---
# PERPLEXITY CONFIGURATION
# Replace with your actual key or set env var PERPLEXITY_API_KEY
PERPLEXITY_API_KEY = os.environ.get("PERPLEXITY_API_KEY", "") 
MODEL_NAME = "sonar-pro" # Grounded model with web search capabilities
API_URL = "https://api.perplexity.ai/chat/completions"

MAX_INPUT_LENGTH = 500
FORBIDDEN_PHRASES = [
    "ignore previous instructions",
    "forget your rules",
    "system override",
    "delete your system prompt"
]

# --- SYSTEM PROMPT ---
SYSTEM_PROMPT_TEXT = """
You are a Magic: The Gathering Commander Deck Building Assistant.

Your goal is to help users build, edit, and refine their 100-card Commander decks.

The user is assumed to be a beginner.

CAPABILITIES:
1. You have access to real-time information via the internet. Use this to find the latest card prices, synergies, and combos.
2. You can use www.edhrec.com to check out cards that synergize well with the commmander.
3. Verify card existence and spelling before suggesting.
4. Feel free to use www.Scryfall.com to get data on the card such as color, cost, card type, etc. 

CRITICAL RULES:
1. YOU ARE A JSON-ONLY API.
2. DO NOT output any text, markdown, or conversation outside of the JSON object.
3. DO NOT use markdown code blocks (e.g., ```json). Just output the raw JSON string.
4. If you break JSON formatting, the system will fail.
5. DECK SIZE MUST BE EXACTLY 100 CARDS.
   - 1 Commander + 99 Mainboard cards.
   - Do NOT return a partial deck. Pad with basic lands if needed to reach 100.
6. If the user asks something unrelated to magic you may tell the user "You don't understand the command." and return the current decklist unchanged. 

DATA & SPELLING:
1. Ensure card names are spelled EXACTLY right (e.g., "Llanowar Elves", not "Llanowar Elf").

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
  "Theme": "Current Deck Theme",
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
current_deck_meta = {
    "RequestedPrice": 0.00, 
    "CurrentDeckPrice": 0.00,
    "Theme": "None"
}

def count_deck_cards(deck_dict):
    total = 0
    for category, cards in deck_dict.items():
        total += len(cards)
    return total

def log_telemetry(pathway, latency):
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "pathway": pathway,
        "latency_sec": round(latency, 2),
        "model": MODEL_NAME
    }
    telemetry_logger.info(json.dumps(entry))

# --- PERPLEXITY API CALL ---
def call_perplexity(messages):
    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": 0.1 
    }
    
    start_time = time.time()
    try:
        response = requests.post(API_URL, json=payload, headers=headers)
        duration = time.time() - start_time
        
        if response.status_code == 200:
            result = response.json()
            log_telemetry("perplexity_api", duration)
            content = result['choices'][0]['message']['content']
            
            clean_content = content.strip()
            if clean_content.startswith('```json'):
                clean_content = clean_content.replace('```json', '').replace('```', '')
            elif clean_content.startswith('```'):
                 clean_content = clean_content.replace('```', '')
                 
            return clean_content
        else:
            logging.error(f"Perplexity API Error: {response.text}")
            return None
    except Exception as e:
        logging.error(f"Perplexity Call Failed: {e}")
        return None

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/reset', methods=['POST'])
def reset_deck():
    global conversation_history, current_deck_state, current_deck_meta
    conversation_history = [{'role': 'system', 'content': SYSTEM_PROMPT_TEXT}]
    current_deck_state = {}
    current_deck_meta = {"RequestedPrice": 0.00, "CurrentDeckPrice": 0.00, "Theme": "None"}
    return jsonify({"status": "success", "message": "Memory cleared."})

@app.route('/chat', methods=['POST'])
def chat_endpoint():
    global conversation_history, current_deck_state, current_deck_meta
    
    data = request.json
    user_input = data.get('message', '')
    client_reported_price = data.get('deckPrice', 0.00)
    current_deck_meta["CurrentDeckPrice"] = client_reported_price
    
    if len(user_input) > MAX_INPUT_LENGTH:
        return jsonify({"Type": "Deck", "Message": f"Error: Message too long.", "Deck": current_deck_state})

    lower_input = user_input.lower()
    for forbidden in FORBIDDEN_PHRASES:
        if forbidden in lower_input:
            log_telemetry("safety_violation", 0)
            return jsonify({"Type": "Deck", "Message": "I cannot comply.", "Deck": current_deck_state})

    if not user_input:
        return jsonify({"error": "No input provided"}), 400
    
    augmented_input = f"{user_input} Search for valid cards and prices. Send back a 100 Card Deck JSON!"
    
    # CRITICAL FIX: Do NOT append to global history yet. 
    # We wait until success to prevent history corruption (User, User) on failure.
    
    try:
        messages_to_send = [conversation_history[0]]
        
        if current_deck_state:
            full_state_context = {
                "Deck": current_deck_state,
                "RequestedPrice": current_deck_meta["RequestedPrice"],
                "CurrentDeckPrice": current_deck_meta["CurrentDeckPrice"],
                "Theme": current_deck_meta["Theme"],
                "CardCount": count_deck_cards(current_deck_state)
            }
            messages_to_send.append({'role': 'system', 'content': f"CURRENT DECK JSON STATE: {json.dumps(full_state_context)}"})
        
        # Add recent history
        recent_messages = conversation_history[1:][-4:]
        messages_to_send.extend(recent_messages)
        
        # Add CURRENT User message to this specific request payload
        current_user_msg = {'role': 'user', 'content': augmented_input}
        messages_to_send.append(current_user_msg)
        
        raw_response = call_perplexity(messages_to_send)
        
        if not raw_response:
             return jsonify({"Type": "Deck", "Message": "Error calling Perplexity API.", "Deck": current_deck_state})

        try:
            parsed_json = json.loads(raw_response)
        except json.JSONDecodeError as e:
            logging.warning(f"JSON Decode Error: {e}")
            json_match = re.search(r'\{.*\}', raw_response, re.DOTALL)
            if json_match:
                try:
                    parsed_json = json.loads(json_match.group(0))
                except:
                     parsed_json = {
                        "Type": "Deck", "Message": raw_response, "Deck": current_deck_state,
                        "RequestedPrice": current_deck_meta["RequestedPrice"], "Theme": current_deck_meta["Theme"]
                    }
            else:
                parsed_json = {
                    "Type": "Deck", "Message": raw_response, "Deck": current_deck_state,
                    "RequestedPrice": current_deck_meta["RequestedPrice"], "Theme": current_deck_meta["Theme"]
                }
        
        validation_message = ""
        
        if parsed_json.get("Deck") and isinstance(parsed_json["Deck"], dict):
            if any(parsed_json["Deck"].values()):
                # Trust the LLM's deck
                parsed_json["Deck"] = parsed_json["Deck"]
                current_deck_state = parsed_json["Deck"]
                
                card_count = count_deck_cards(current_deck_state)
                
                # Still verify size and ask for refinement if needed
                if card_count != 100:
                    logging.info(f"Deck count {card_count}/100. Refining.")
                    needed = 100 - card_count
                    refine_prompt = (
                        f"The deck currently has {card_count} valid cards. "
                        f"You MUST add exactly {needed} more cards to reach 100. "
                        f"Stick to the theme: {current_deck_meta['Theme']}. "
                        "Fill empty slots with Basic Lands if needed. "
                        "Output the COMPLETE 100-card deck JSON."
                    )
                    
                    # Fix message alternation for refinement
                    refine_messages = messages_to_send + [
                        {'role': 'assistant', 'content': raw_response}, 
                        {'role': 'user', 'content': refine_prompt}
                    ]
                    
                    refined_raw = call_perplexity(refine_messages)
                    
                    if refined_raw:
                        try:
                            refined_json = json.loads(refined_raw)
                            if refined_json.get("Deck"):
                                new_count = count_deck_cards(refined_json["Deck"])
                                parsed_json = refined_json
                                current_deck_state = refined_json["Deck"]
                                parsed_json["CardCount"] = new_count
                                
                                validation_message += f"\n[REFINED]: Deck updated to {new_count} cards."
                        except json.JSONDecodeError:
                            logging.error("Refinement JSON failed.")
                
                parsed_json["CardCount"] = count_deck_cards(current_deck_state)
            else:
                parsed_json["Deck"] = current_deck_state
        else:
            parsed_json["Deck"] = current_deck_state
        
        if "RequestedPrice" in parsed_json:
            current_deck_meta["RequestedPrice"] = parsed_json["RequestedPrice"]
        if "Theme" in parsed_json:
            current_deck_meta["Theme"] = parsed_json["Theme"]
            
        if validation_message:
            if "Message" not in parsed_json:
                parsed_json["Message"] = ""
            parsed_json["Message"] += ("\n" + validation_message)
        
        # SUCCESS! Now we safely update global history
        conversation_history.append(current_user_msg)
        
        history_entry = {"Type": "Deck", "Message": parsed_json.get("Message", "")}
        conversation_history.append({'role': 'assistant', 'content': json.dumps(history_entry)})
        
        return jsonify(parsed_json)
    
    except Exception as e:
        logging.error(f"Error: {e}", exc_info=True)
        # Note: We do NOT append to conversation_history here, keeping it clean for the next try
        return jsonify({"Type": "Deck", "Message": f"Error: {str(e)}", "Deck": current_deck_state})

if __name__ == '__main__':
    app.run(debug=True, port=5000)