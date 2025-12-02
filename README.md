# Commander Architect: AI-Powered MTG Deck Builder

**Commander Architect** is a web-based application that utilizes Large Language Models (LLMs) to generate, refine, and visualize Magic: The Gathering Commander decks. 

It features two backend implementations:
1.  **Local Mode (`app.py`):** Uses **Ollama** (e.g., Qwen, Gemma) for privacy and local processing, augmented with **Scryfall** (validation) and **EDHREC** (synergy) APIs.
2.  **Cloud Mode (`app_api.py`):** Uses the **Perplexity API** (Sonar Pro) for grounded, real-time web search capabilities.

## Features

* **Natural Language Deck Building:** "Build me a $50 Golgari Elves deck" or "Make a Jodah deck themed around legendary chairs."
* **Real-Time Pricing:** Fetches live pricing via Scryfall.
* **Visual Grid:** View card art in a responsive grid.
* **Strict Validation (Local Mode):** Auto-corrects card spelling and enforces Commander color identity rules.
* **Export:** Download decklists in standard `.txt` format for import into TCGPlayer or Moxfield.

---

## Installation & Setup

### Prerequisites
* Python 3.8+
* [Ollama](https://ollama.com/) (Only required for Local Mode)

### 1. Clone or Download
Download the project files (`index.html`, `app.py`, `app_api.py`) into a folder.

### 2. Set up Virtual Environment
It is recommended to run this project in an isolated environment.

**Windows:**
```bash
python -m venv venv
.\venv\Scripts\activate
```
### 3. Install dependencies
```
pip install flask requests ollama
```
### 4. LLM Setup (Local Mode Only)
If you plan to use app.py, ensure you have Ollama installed and pull the model defined in the code (default is qwen2.5:14b).
```
ollama pull qwen2.5:14b
```
# Or if you change the code to use gemma:
```
ollama pull gemma2
```
### 5. API Setup (Cloud Mode Only)
If you plan to use app_api.py, set your Perplexity API key in the app_api.py script
line 24 in app_api
PERPLEXITY_API_KEY = os.environ.get("PERPLEXITY_API_KEY", "<POST API KEY HERE>") 

Option A: Local Mode (Ollama + Scryfall + EDHREC)
This mode runs entirely on your machine (except for lightweight API calls for card data).

```
python app.py
```

Access: Open http://127.0.0.1:5001 in your browser.

Option B: Cloud Mode (Perplexity API)
This mode offloads the "thinking" to Perplexity's servers.

```
python app_api.py
```

Access: Open http://127.0.0.1:5000 in your browser


## Usage
1. Enter a prompt in the chat box (e.g., "Build a Dinosaur deck with Gishath as commander, budget $100").

2. Wait for the AI to generate the JSON.

3. The frontend will automatically fetch card images and prices.

4. Use Visual Mode to see art or List Mode for a compact view.

5. Click Export to save your decklist.


TECHNICAL NOTE
===================================
1. ARCHITECTURE DIAGRAM (Local Implementation)
-----------------------------------------------
```
User Client (Browser)
      |
      | HTTP POST /chat (JSON)
      v
+------------------------+
|      FLASK SERVER      |
|       (app.py)         |
+------------------------+
      |
      +---> [1. Context Builder] adds System Prompt + History
      |
      +---> [2. EDHREC Tool] fetches synergy cards for Commander
      |
      v
+------------------------+
|      OLLAMA (LLM)      | <--- Generates Initial Deck JSON
|     (Qwen/Gemma)       |
+------------------------+
      |
      v
+------------------------+       +-------------------------+
|   VALIDATION LAYER     | <---> | SCYFALL API (External)  |
+------------------------+       +-------------------------+
      |  - Bulk Check (/collection)
      |  - Fuzzy Rescue (/search?q=)
      |  - Color Identity Check
      |
      +---> [Auto-Refine] If Deck < 100 cards, re-prompt LLM
      |
      v
HTTP Response (Validated JSON) -> Updates Frontend DOM
```
 GUARDRAILS & SAFETY
-----------------------
A. Input Sanitization:
   - Max input length enforced (1000 chars) to prevent context overflow.
   - Forbidden phrases list (e.g., "ignore previous instructions") to prevent prompt injection.

B. Output Structuring:
   - System Prompt enforces strict JSON-only output.
   - Regex fallback in `app_api.py` attempts to extract JSON if the LLM adds conversational fluff.

C. Hallucination Control (Zero Hallucination Policy):
   - The "Rescue Logic" in `app.py` checks Scryfall. If a card doesn't exist, it attempts a fuzzy search.
   - If fuzzy search fails, the card is flagged or removed.
   - Color Identity validation ensures suggested cards are legal for the specific Commander.

3. EVALUATION METHODS
---------------------
The system utilizes a telemetry logger (`app_telemetry.jsonl`) to track:

1. Latency: Time taken for the LLM to generate the deck + validation time.
2. Parsing Success: Frequency of JSONDecodeErrors.
3. Deck Completeness: Does the output hit exactly 100 cards? (Refinement loop trigger rate).
4. Tool Efficiency: How often Scryfall rescue logic is required (indicates LLM spelling accuracy).

4. KNOWN LIMITATIONS
--------------------
1. Token Memory: Conversation history is truncated to the last 4 turns. The LLM may "forget" early constraints if the conversation is long.
2. Basic Land Padding: If the LLM struggles to find specific cards to reach the 100-card limit, it often defaults to filling the remaining slots with basic lands, skewing the mana base.
3. Image Loading: The frontend fetches images client-side from Scryfall. Large decks (100 distinct cards) may hit browser rate limits or cause visual "pop-in."
4. Pricing Accuracy: The LLM "estimates" budget during generation, but real prices are only calculated post-generation by the frontend. A user asking for a $50 deck might get a $70 deck if card prices have spiked.