# V000-000-002
import time
import re
import string

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
import faiss
import nltk
from nltk.stem import WordNetLemmatizer

# --- one-time NLTK setup (ideally move to environment bootstrap) ---
for resource in ["wordnet", "omw-1.4"]:
    try:
        nltk.data.find(f"corpora/{resource}")
    except LookupError:
        nltk.download(resource)

lemmatizer = WordNetLemmatizer()

# --- 1. Normalization tailored to bathroom remodeling ---

def normalize_text(text: str) -> str:
    if not isinstance(text, str):
        return ""

    # Lowercase
    text = text.lower()

    # Basic punctuation removal (keep spaces)
    text = text.translate(str.maketrans("", "", string.punctuation))

    # Domain-specific replacements (adjust as needed)
    replacements = {
        # common variants
        r"\bbath rm\b": " bathroom ",
        r"\bbathrm\b": " bathroom ",
        r"\bbath\b": " bathroom ",
        r"\breno\b": " remodel ",
        r"\brenovation\b": " remodel ",
        r"\brenovations\b": " remodel ",
        r"\bremodeling\b": " remodel ",
        r"\bremodelled\b": " remodel ",
        r"\bremodels\b": " remodel ",

        # shower / tub
        r"\btub to shower\b": " tub to shower ",
        r"\btub 2 shower\b": " tub to shower ",
        r"\bwalk in shower\b": " walk in shower ",
        r"\bwalkin shower\b": " walk in shower ",

        # spelling variants if relevant
        r"\bcolour\b": " color ",
        r"\bfibre\b": " fiber ",
    }

    for pattern, repl in replacements.items():
        text = re.sub(pattern, repl, text)

    # Tokenize on whitespace
    tokens = text.split()

    # Lemmatize
    tokens = [lemmatizer.lemmatize(tok) for tok in tokens]

    # (optional) remove very generic words – be conservative
    stopwords = {"for", "and", "in", "of", "to", "a", "the"}
    tokens = [t for t in tokens if t not in stopwords]

    return " ".join(tokens)

def get_intent_category(term: str) -> str:
    """
    Classify a normalized term into a broad intent bucket.
    Used to prevent merging 'shower' and 'bathroom' queries.
    """
    if not isinstance(term, str):
        return "other"

    tokens = term.split()

    has_shower = "shower" in tokens
    has_bathroom = "bathroom" in tokens
    has_tub = "tub" in tokens
    has_kitchen = "kitchen" in tokens

    # You can customize these rules as needed.
    if has_shower and not has_bathroom:
        return "shower_only"
    if has_bathroom and not has_shower:
        return "bathroom_only"
    if has_shower and has_bathroom:
        return "mixed_bath_shower"
    if has_tub and not (has_shower or has_bathroom):
        return "tub_only"
    if has_kitchen:
        return "kitchen"

    return "other"


# ------------------ Grouping function

GROUP_PATTERNS = [
    # --- Brand ---
    ("rebath", r"\bre ?bath\b"),

    # --- 1 day / 24 hour ---
    ("1 day", r"\b(1 day|one day|24 hour)\b"),

    # --- Bathtub conversion (generic) ---
    ("bathtub conversion", r"\bbathtub conversion\b"),

    # --- Bathtub conversion cost ---
    ("bathtub conversion cost", r"\bbathtub conversion cost\b"),

    # --- Fitter shower (tub, bath) ---
    # covers "fitter shower", and variants mentioning tub/bath with "fitter"
    ("fitter shower", r"\bfitter (?:shower|tub|bath)\b"),

    # --- Shower/Tub remodeling / renovation ---
    ("shower/tub remodeling", r"\b(shower|tub) (remodel|remodeling|renovation|reno)\b"),

    # --- Bathroom remodel near me (in my area, local) ---
    ("bathroom remodel near me", r"\bbathroom (remodel|remodeling)\b.*\b(near me|in my area|local)\b"),

    # --- Shower/Tub remodel near me (in my area, local) ---
    ("shower/tub remodel near me", r"\b(shower|tub) (remodel|remodeling)\b.*\b(near me|in my area|local)\b"),

    # --- Bathroom repair ---
    ("bathroom repair", r"\bbathroom repair\b"),

    # --- Bathroom upgrades / update ---
    ("bathroom upgrades", r"\bbathroom (upgrade|upgrades|update|updates)\b"),

    # --- Redo shower ---
    ("redo shower", r"\bredo shower\b"),

    # --- Shower/Tub installation ---
    ("shower/tub installation", r"\b(shower|tub) installation\b"),

    # --- Bathroom conversion ---
    ("bathroom conversion", r"\bbathroom conversion\b"),

    # --- Bathroom addition (construction, add) ---
    ("bathroom addition", r"\bbathroom addition\b|\b(add|addition|construction) bathroom\b"),

    # --- Bathroom remodel contractors ---
    ("bathroom remodel contractors", r"\bbathroom (remodel|remodeling)\b.*\b(contractor|contractors)\b"),

    # --- Cost bathroom ---
    ("cost bathroom", r"\b(cost|price|pricing)\b.*\bbathroom\b"),

    # --- Bathroom contractors ---
    ("bathroom contractors", r"\bbathroom\b.*\b(contractor|contractors)\b"),

    # --- Bathroom remodel (generic, no 'near me') ---
    ("bathroom remodel", r"\bbathroom (remodel|remodeling)\b"),

    # --- Bathroom makeover ---
    ("bathroom makeover", r"\bbathroom makeover\b"),

    # --- Shower makeover ---
    ("shower makeover", r"\bshower makeover\b"),

    # --- Kitchen and bathroom (bath) ---
    # Make sure kitchen terms are kept separate from generic bathroom-only terms
    ("kitchen and bathroom", r"\bkitchen\b.*\b(bath|bathroom)\b"),
]


def assign_group(text: str) -> str:
    """
    Assign a keyword_group based on rule-based patterns.
    Returns 'unassigned' if no pattern matches.
    """
    if not isinstance(text, str):
        return "unassigned"

    t = text.lower()

    # IMPORTANT: kitchen vs bath split first, if you want
    # to ensure 'kitchen and bath remodeling near me' goes into
    # the kitchen/bath bucket and not the generic bathroom ones.
    # (This is already mostly handled by the explicit 'kitchen and bathroom'
    # pattern above, but you could add extra kitchen-only logic here if needed.)

    for group_name, pattern in GROUP_PATTERNS:
        if re.search(pattern, t):
            return group_name

    return "unassigned"


def find_top_words_in_keyword(text: str, top_words_set) -> list[str]:
    """
    Given a keyword phrase and a set of high-frequency words,
    return the list of those high-frequency words that appear
    as individual tokens in the keyword.
    """
    if not isinstance(text, str):
        return []

    tokens = text.lower().split()
    # Use a set to avoid double-counting the same word in the same keyword
    matched = sorted({tok for tok in tokens if tok in top_words_set})
    return matched


def main():
    overall_start = time.time()

    # --- 2. Load the Keyword Planner workbook ---
    input_file = "data/test_dataset_keywordplanner.xlsx"
    sheet_name = "keyterm"

    print(f"Loading data from {input_file}, sheet '{sheet_name}'...")
    # header=2 because row 3 contains the column names
    df = pd.read_excel(input_file, sheet_name=sheet_name, header=2)
    print(f"Loaded {len(df):,} rows and {len(df.columns)} columns.")
    
    
    # --- 2b. Load word frequency file and get top 70 words ---
    freq_file = "output/bathroom_keywordplanner_word_frequency.csv"  # adjust path if needed
    print(f"Loading word frequency data from {freq_file}...")
    freq_df = pd.read_csv(freq_file)

    # Ensure the key columns exist
    # Columns: Word, Frequency, Total Avg. monthly searches
    freq_df["Word"] = freq_df["Word"].astype(str).str.lower()

    # Sort by Total Avg. monthly searches descending and take top 70
    freq_df = freq_df.sort_values("Total Avg. monthly searches", ascending=False)
    top_n = 70
    top_words = freq_df.head(top_n)["Word"].tolist()
    top_words_set = set(top_words)

    print(f"Selected top {top_n} words based on Total Avg. monthly searches.")


    # --- 3. Normalize the keyword text ---
    print("Normalizing Keyword text...")
    t0 = time.time()
    df["keyword_normalized"] = df["Keyword"].apply(normalize_text)
    print(f"Normalization completed in {time.time() - t0:.2f} seconds.")

    # If you only want to cluster unique normalized strings:
    unique_terms = df["keyword_normalized"].dropna().unique()
    print(f"Unique normalized terms: {len(unique_terms):,}")

    # --- 4. TF-IDF vectorization over unique normalized terms ---
    print("Vectorizing normalized terms with TF-IDF...")
    t0 = time.time()
    vectorizer = TfidfVectorizer(
        ngram_range=(1, 2),       # unigrams + bigrams for better phrase capture
        min_df=1,
    )
    tfidf_matrix = vectorizer.fit_transform(unique_terms)
    tfidf_dense = tfidf_matrix.astype(np.float32).toarray()
    print(f"TF-IDF shape (unique terms): {tfidf_dense.shape}, done in {time.time() - t0:.2f} seconds.")

    # --- 5. Build Faiss index and search neighbors ---
    print("Building Faiss index and searching neighbors...")
    t0 = time.time()
    dim = tfidf_dense.shape[1]
    index = faiss.IndexFlatL2(dim)
    index.add(tfidf_dense)

    k = 5  # up to 5 neighbors
    distances, indices = index.search(tfidf_dense, k)
    print(f"Faiss search completed in {time.time() - t0:.2f} seconds.")

    # --- 6. Choose canonical term per similarity cluster ---
    print("Selecting canonical term per group...")
    t0 = time.time()

    # Frequency of each normalized term in the full dataset
    freq = df["keyword_normalized"].value_counts().to_dict()

    # Map from normalized term -> canonical term
    canonical_map = {}

    # Optional: diagnostics table
    similarity_rows = []

    # Distance threshold: tune this with your data
    max_distance = 0.6  # more tolerant than 0.2

    for i, term in enumerate(unique_terms):
        if term in canonical_map:
            # already assigned via another neighbor
            continue

        base_cat = get_intent_category(term)

        neighbor_indices = indices[i]
        neighbor_distances = distances[i]

        # Collect this term + close neighbors
        group_terms = {term}
        for j, d in zip(neighbor_indices[1:], neighbor_distances[1:]):
            if d > max_distance:
                continue

            neighbor_term = unique_terms[j]
            neighbor_cat = get_intent_category(neighbor_term)

            # NEW RULE: don't merge across intent categories
            # (e.g., shower_only vs bathroom_only)
            if base_cat != neighbor_cat:
                continue

            group_terms.add(neighbor_term)
            similarity_rows.append({
                "term": term,
                "neighbor": neighbor_term,
                "l2_distance": float(d),
            })


        # Choose most frequent term in the group as canonical
        canonical = max(group_terms, key=lambda t: freq.get(t, 0))

        # Assign canonical for each term in the group
        for t in group_terms:
            canonical_map[t] = canonical

    print(f"Canonical mapping created for {len(canonical_map):,} normalized terms in {time.time() - t0:.2f} seconds.")

    # --- 7. Apply canonical mapping back to the full dataframe ---
    print("Applying canonical terms to full dataset...")
    t0 = time.time()
    df["keyword_final"] = df["keyword_normalized"].map(canonical_map).fillna(df["keyword_normalized"])
    print(f"Applied canonical mapping in {time.time() - t0:.2f} seconds.")
    
    # --- 7b. Apply rule-based grouping ---
    print("Assigning keyword groups...")
    t0 = time.time()
    df["keyword_group"] = df["keyword_final"].apply(assign_group)
    print(f"Assigned keyword groups in {time.time() - t0:.2f} seconds.")
    
        # --- 7c. Find high-frequency words in each keyword ---
    print("Identifying top-frequency words in each keyword...")
    t0 = time.time()

    # List of top frequency words present in each keyword
    df["top_freq_words_list"] = df["keyword_final"].apply(
        lambda txt: find_top_words_in_keyword(txt, top_words_set)
    )

    # Stringified version for easier viewing / export
    df["top_freq_words_str"] = df["top_freq_words_list"].apply(
        lambda lst: ", ".join(lst) if lst else ""
    )

    # Flag: does this keyword contain any of the top N words?
    df["has_top_freq_word"] = df["top_freq_words_list"].apply(lambda lst: len(lst) > 0)

    # Exact-match candidate:
    # keyword_final is a single word AND that word is in the top set
    def is_exact_top_word(txt: str) -> bool:
        if not isinstance(txt, str):
            return False
        tokens = txt.lower().split()
        return len(tokens) == 1 and tokens[0] in top_words_set

    df["is_exact_top_word"] = df["keyword_final"].apply(is_exact_top_word)

    # Phrase-match candidate:
    # contains at least one top word but is not a pure exact single-word match
    df["is_phrase_match_candidate"] = df.apply(
        lambda row: bool(row["has_top_freq_word"] and not row["is_exact_top_word"]),
        axis=1,
    )

    # Optional: a single label column for convenience
    df["match_type"] = np.where(
        df["is_exact_top_word"],
        "exact_top_word",
        np.where(
            df["is_phrase_match_candidate"],
            "phrase_candidate",
            "none"
        ),
    )

    print(f"Top-frequency word tagging completed in {time.time() - t0:.2f} seconds.")

    # --- 8. Save outputs, preserving all original columns ---
    output_excel = "output/keywordplanner_grouped.xlsx"
    output_similarity = "output/keyword_similarity_pairs.csv"

    print(f"Saving grouped data to {output_excel}...")
    df.to_excel(output_excel, index=False)

    print(f"Saving similarity diagnostics to {output_similarity}...")
    if similarity_rows:
        sim_df = pd.DataFrame(similarity_rows)
        sim_df.to_csv(output_similarity, index=False)
    else:
        # avoid creating an empty file if no pairs passed the threshold
        print("No similarity pairs under the threshold; skipping similarity file.")

    print(f"Done. Total runtime: {time.time() - overall_start:.2f} seconds.")


if __name__ == "__main__":
    main()
