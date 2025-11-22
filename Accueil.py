import streamlit as st
import json
import os
import time
# 🌟 Ajout de la librairie Gemini SDK
from google import genai
from google.genai.errors import APIError
import bcrypt
from PIL import Image
from io import BytesIO
from datetime import date
from supabase import create_client, Client
from streamlit_cookies_manager import EncryptedCookieManager
from urllib.parse import urlparse, parse_qs

# --- I. Configuration Globale ---

st.set_page_config(
    page_title="Tuteur IA Mathématiques (Système Marocain) 🇲🇦", # 🎨 Ajout émoji
    layout="wide",
    initial_sidebar_state="expanded"
)

# Constantes et Secrets
MAX_REQUESTS = 5
REFERRAL_BONUS = 10
REFERRAL_PARAM = "ref_code"
COOKIE_KEY_EMAIL = "user_auth_email"
SUPABASE_TABLE_NAME = "users"
ADMIN_EMAIL = "ahmed.tantawi.10@gmail.com" # Utilisez votre email ici

# Configuration des API Keys depuis secrets.toml
try:
    API_KEY = st.secrets["GEMINI_API_KEY"]
    SUPABASE_URL: str = st.secrets["SUPABASE_URL"]
    SUPABASE_KEY: str = st.secrets["SUPABASE_KEY"]
    SERVICE_KEY = st.secrets.get("SUPABASE_SERVICE_KEY")
except KeyError as e:
    st.error(f"❌ Erreur de configuration: Clé manquante dans secrets.toml: {e}. L'application ne démarrera pas correctement.") 
    st.stop()
    
# 🌟 Initialisation du client Gemini SDK
try:
    GEMINI_CLIENT = genai.Client(api_key=API_KEY)
except Exception as e:
    st.error(f"💥 Erreur d'initialisation Gemini SDK: {e}") 
    st.stop()

# Liste des niveaux scolaires marocains
MAROC_LEVELS = [
    'الإعدادي (Collège)',
    'جذع مشترك (Tronc Commun)',
    'الأولى بكالوريا (1ère Année Bac)',
    'الثانية بكالوريا (2ème Année Bac)',
    'الدروس الخصوصية (Classes Préparatoires)',
]

# 🌟 Ajout: Options de types de réponse
RESPONSE_TYPES = {
    'steps': 'Étapes Détaillées (Didactique) 🔢', 
    'concept': 'Explication Conceptuelle (Théorie) 🧠', 
    'answer': 'Réponse Finale (Concise) ✅' 
}


# --- II. Initialisation des Clients et de l'État ---

# 1. Initialisation des Cookies
cookies = EncryptedCookieManager(
    prefix="gemini_math_app/",
    password=st.secrets.get("COOKIE_PASSWORD", "super_secret_default_key"),
)
if not cookies.ready():
    st.stop()

# 2. Initialisation Supabase Client
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    users_table = supabase.table(SUPABASE_TABLE_NAME)
except Exception as e:
    st.error(f"💾 Erreur d'initialisation Supabase: {e}") 
    st.stop()
    
# 3. Initialisation de l'État de la Session
if 'auth_status' not in st.session_state: st.session_state.auth_status = 'logged_out'
if 'user_email' not in st.session_state: st.session_state.user_email = None
if 'user_data' not in st.session_state: st.session_state.user_data = None
if 'requests_today' not in st.session_state: st.session_state.requests_today = 0
if 'is_unlimited' not in st.session_state: st.session_state.is_unlimited = False
if 'should_rerun' not in st.session_state: st.session_state.should_rerun = False
if 'school_level' not in st.session_state: st.session_state.school_level = MAROC_LEVELS[-1] 
if 'response_type' not in st.session_state: st.session_state.response_type = 'steps'
if 'lang' not in st.session_state: st.session_state.lang = 'fr'


# --- III. Fonctions de Base (Supabase & Crypto) 🔑 --- 

def get_supabase_client(use_service_key: bool = False) -> Client:
    """Retourne le client Supabase standard ou le client avec clé de service."""
    if use_service_key and SERVICE_KEY:
        return create_client(SUPABASE_URL, SERVICE_KEY)
    return supabase

def hash_password(password: str) -> str:
    """Hachage sécurisé du mot de passe avec bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def check_password(password: str, hashed_password: str) -> bool:
    """Vérifie le mot de passe entré."""
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed_password.encode('utf-8'))
    except Exception:
        return False

def get_user_by_email(email: str):
    """Récupère les données utilisateur."""
    try:
        response = users_table.select("*").eq("email", email).limit(1).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Erreur de récupération utilisateur: {e}")
        return None

def update_user_data(email, data: dict, use_service_key=False):
    """Met à jour les données utilisateur."""
    try:
        client_to_use = get_supabase_client(use_service_key)
        response = client_to_use.table(SUPABASE_TABLE_NAME).update(data).eq("email", email).execute()
        
        if response.data and st.session_state.user_email == email:
            # Mise à jour de la session si l'utilisateur actuel est modifié
            st.session_state.user_data.update(response.data[0])
        return True
    except Exception as e:
        print(f"Erreur de mise à jour Supabase: {e}")
        return False


# --- IV. Logique de l'API Gemini 🤖 --- 

def build_system_prompt():
    """
    Construit la System Instruction complète.
    Inclut des instructions strictes de formatage pour des réponses Tidy/Clean.
    """
    # Utilisation des valeurs de session_state directement
    school_level = st.session_state.school_level
    response_type = st.session_state.response_type
    lang = st.session_state.lang

    # Base: Spécialisation et niveau
    base_prompt = (
        f"Tu es un tuteur spécialisé en mathématiques, expert du système éducatif marocain (niveau {school_level}). "
        "Ta mission est de fournir une assistance précise et didactique. Si une image est fournie, tu dois l'analyser et résoudre le problème. "
        "Si une image est fournie, commence par une description concise du problème (en utilisant la langue de réponse choisie) avant de passer à la résolution structurée."
    )
    
    # Style de réponse
    if response_type == 'answer':
        style_instruction = "Fournis uniquement la réponse finale et concise du problème, sans aucune explication détaillée ni étapes intermédiaires. Mets la réponse en gras et clairement en évidence."
    elif response_type == 'concept':
        style_instruction = "Fournis une explication conceptuelle approfondie du problème ou du sujet. Concentre-toi sur les théories et les concepts impliqués, et utilise des sous-titres clairs pour séparer les notions."
    else: # 'steps' par défaut
        style_instruction = "Fournis les étapes détaillées de résolution de manière structurée et méthodique, en utilisant une liste numérotée pour chaque étape majeure du raisonnement."

    # Langue
    lang_instruction = "Tu dois répondre exclusivement en français." if lang == 'fr' else "Tu dois répondre exclusivement en arabe, au format (Markdown) et en utilisant les termes mathématiques habituels au Maroc."
    
    # Emojis
    emoji_instruction = (
        "**CRUCIAL:** Intègre des **émojis pertinents et visuellement attrayants** (comme ➕, ✖️, 💡, 📐, etc.) dans le corps de ta réponse pour la rendre plus engageante et claire. Place-les au début des points importants ou des sections."
    )

    # Instruction STRICTE de mise en forme (Tidiness/Clarity) 🌟 
    formatting_instruction = (
        "Réponds IMPÉRATIVEMENT en utilisant une structure **Markdown** claire (titres, listes, gras). "
        "Utilise des titres de niveau 2 ('##') pour les sections principales et de niveau 3 ('###') pour les sous-sections. "
        "**Il est crucial de laisser DEUX sauts de ligne consécutifs (c'est-à-dire une ligne vide) entre chaque titre, chaque paragraphe, et chaque bloc de texte indépendant pour assurer un espacement clair et une lisibilité maximale.** "
        "**Interdiction absolue d'utiliser des balises HTML, y compris <br>, <p> ou <div>, pour le formatage ou l'espacement. Fais confiance uniquement aux sauts de ligne Markdown.** " 
        "Toutes les expressions mathématiques complexes, symboles, formules ou équations doivent être écrites UNIQUEMENT en **LaTeX**. "
        "Utilise le format LaTeX : encadre les équations en ligne avec '$' et les blocs d'équations avec '$$'. "
        "Il est INTERDIT d'utiliser du texte brut, des barres obliques (/) ou des accents circonflexes (^) pour représenter des fractions, des exposants ou des symboles mathématiques dans la réponse finale."
    )
    
    # Instruction finale complète
    final_prompt = (
        f"{base_prompt} {lang_instruction} {style_instruction} {emoji_instruction} {formatting_instruction}"
    )
    return final_prompt

# 🌟 Fonction call_gemini_api utilisant le SDK 🌟
def call_gemini_api(prompt: str, uploaded_file=None):
    """Appelle l'API Gemini en utilisant le SDK."""
    
    email = st.session_state.user_email
    user_data = st.session_state.user_data
    current_date_str = str(date.today())
    
    # 1. Vérification des Limites
    max_total_requests = MAX_REQUESTS + user_data.get('bonus_questions', 0)
    if not user_data.get('is_unlimited', False):
        
        # Réinitialisation du compteur si la date a changé
        if user_data.get('last_request_date') != current_date_str:
            st.session_state.requests_today = 0
            update_user_data(email, {'requests_today': 0, 'last_request_date': current_date_str})

        current_count = st.session_state.requests_today

        if current_count >= max_total_requests:
            st.error(f"🛑 Limite atteinte: Vous avez atteint le maximum de requêtes ({max_total_requests}) pour aujourd'hui. Revenez demain!") 
            return "Limite de requêtes atteinte.", []
            
        st.session_state.requests_today = current_count + 1 # Incrément avant l'appel

    # 2. Construction du contenu et des instructions
    final_system_prompt = build_system_prompt()
    contents = []
    
    if uploaded_file is not None:
        try:
            # SDK accepte un objet PIL.Image directement
            uploaded_file.seek(0) 
            image = Image.open(uploaded_file)
            contents.append(image)
        except Exception:
            return "⚠️ Impossible de traiter l'image. Assurez-vous que le format est JPG ou PNG.", [] 
    
    if prompt: 
        contents.append(prompt)
        
    if not contents:
        return "Veuillez fournir une question ou une image.", []

    # 3. Appel API avec SDK
    try:
        response = GEMINI_CLIENT.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents,
            config={
                "system_instruction": final_system_prompt,
                "tools": [{"google_search": {} }]
            }
        )
        
        # 4. Mise à jour du compteur dans Supabase
        if not user_data.get('is_unlimited', False):
            update_user_data(email, {'requests_today': st.session_state.requests_today, 'last_request_date': current_date_str})
            
        # 5. Extraction de la réponse et des sources
        generated_text = response.text
        
        sources = []
        if (response.candidates and 
            response.candidates[0].grounding_metadata and 
            hasattr(response.candidates[0].grounding_metadata, 'grounding_attributions')):

            for attribution in response.candidates[0].grounding_metadata.grounding_attributions:
                if hasattr(attribution, 'web') and attribution.web and attribution.web.title:
                    sources.append({
                        'uri': attribution.web.uri,
                        'title': attribution.web.title
                    })

        return generated_text, sources

    except APIError as e:
        st.error(f"❌ Erreur API (Code {e.code}): {e.message}") 
        return f"Échec de l'API Gemini (Code {e.code}). Cause probable: {e.message}", []
        
    except Exception as e:
        st.error(f"💥 Erreur inattendue: {e}") 
        return f"Erreur inattendue: {e}", []

# --- V. Fonctions d'Authentification et de Session 👤 --- 

def load_user_session(email, save_cookie=False):
    """Charge les données utilisateur et met à jour la session."""
    user_data = get_user_by_email(email)
    
    if user_data:
        if save_cookie:
            cookies[COOKIE_KEY_EMAIL] = email
            cookies.save()
            
        st.session_state.user_email = email
        st.session_state.user_data = user_data
        
        # Copie des préférences dans session_state
        st.session_state.school_level = user_data.get('school_level', MAROC_LEVELS[-1])
        st.session_state.response_type = user_data.get('response_type', 'steps')
        st.session_state.lang = user_data.get('lang', 'fr')
        
        # Préférences utilisateur
        st.session_state.is_unlimited = user_data.get('is_unlimited', False)
        
        # Gestion du compteur quotidien 
        current_date_str = str(date.today())
        if user_data.get('last_request_date') != current_date_str:
            st.session_state.requests_today = 0
        else:
            st.session_state.requests_today = user_data.get('requests_today', 0)
            
        st.session_state.auth_status = 'logged_in'
        st.session_state.should_rerun = True
        return True
    return False

def handle_login():
    """Gère la connexion."""
    email = st.session_state.login_email.lower()
    password = st.session_state.login_password
    
    user_data = get_user_by_email(email)
    
    if user_data and check_password(password, user_data.get('password_hash', '')):
        st.success("🎉 Connexion réussie! Bienvenue.") 
        load_user_session(email, save_cookie=True)
    else:
        st.error("⚠️ E-mail ou mot de passe incorrect.") 

def handle_register():
    """Gère l'inscription et le Parrainage."""
    email = st.session_state.reg_email.lower()
    password = st.session_state.reg_password
    confirm_password = st.session_state.reg_password_confirm
    # Extraction des options
    selected_level = st.session_state.reg_level
    selected_lang = st.session_state.reg_lang
    selected_response_type = st.session_state.reg_response_type
    
    if password != confirm_password:
        st.error("⚠️ Les mots de passe ne correspondent pas.") 
        return
    if len(password) < 6:
        st.error("⚠️ Le mot de passe doit contenir au moins 6 caractères.") 
        return
        
    if get_user_by_email(email):
        st.error("⚠️ Cet e-mail est déjà enregistré. Veuillez vous connecter.") 
        return

    # Logique de Parrainage
    referrer_email = None
    query_params = st.query_params
    
    if REFERRAL_PARAM in query_params:
        potential_referrer_email = query_params.get(REFERRAL_PARAM)
        if isinstance(potential_referrer_email, list): potential_referrer_email = potential_referrer_email[0]
            
        referrer_data = get_user_by_email(potential_referrer_email)
        if referrer_data and referrer_data['email'] != email: 
            referrer_email = potential_referrer_email
            current_bonus = referrer_data.get('bonus_questions', 0)
            new_bonus = current_bonus + REFERRAL_BONUS
            
            # Utilisation de la clé de service pour l'écriture
            if update_user_data(referrer_email, {'bonus_questions': new_bonus}, use_service_key=True):
                st.info(f"🌟 Félicitations! Le parrain ({referrer_email}) a reçu {REFERRAL_BONUS} questions bonus.") 
            
    # Sauvegarder le nouvel utilisateur
    new_user_data = {
        'email': email,
        'password_hash': hash_password(password),
        'lang': selected_lang,
        'school_level': selected_level, 
        'response_type': selected_response_type, 
        'is_unlimited': False,
        'requests_today': 0,
        'last_request_date': str(date.today()),
        'bonus_questions': 0,
        'referred_by': referrer_email,
    }
    
    try:
        users_table.insert([new_user_data]).execute()
        st.success("🚀 Inscription et connexion réussies! 🥳") 
        load_user_session(email, save_cookie=True)
    except Exception as e:
        st.error(f"❌ Échec de l'inscription: {e}. (Vérifiez les règles RLS de Supabase.)") 


# --- VI. Interface Utilisateur (UI) 🖥️ --- 

def admin_dashboard_ui():
    """Tableau de bord administrateur."""
    st.sidebar.markdown("---")
    st.sidebar.subheader("👑 Panneau Admin")
    st.sidebar.warning("🚨 Visible uniquement par vous.") 
    
    st.sidebar.markdown(f"**Email Admin:** `{ADMIN_EMAIL}`")
    
    if st.sidebar.button("Activer/Désactiver Illimité (Moi)"):
        is_current_unlimited = st.session_state.user_data.get('is_unlimited', False)
        new_status = not is_current_unlimited
        
        if update_user_data(ADMIN_EMAIL, {'is_unlimited': new_status}, use_service_key=True):
            st.session_state.is_unlimited = new_status
            st.session_state.should_rerun = True
            st.sidebar.success(f"Statut Illimité: {'✅ Activé' if new_status else '❌ Désactivé'}") 
        else:
            st.sidebar.error("💥 Échec de la mise à jour.") 
            
    st.sidebar.markdown("---")


def auth_ui():
    """Interface de connexion/inscription."""
    st.header("🔑 Connexion / Inscription") 
    st.markdown("---")

    col1, col2 = st.columns(2)
    
    with col1:
        with st.form("login_form"):
            st.subheader("Se Connecter ➡️") 
            st.text_input("E-mail", key="login_email")
            st.text_input("Mot de passe", type="password", key="login_password")
            st.form_submit_button("Connexion", type="primary", on_click=handle_login)

    with col2:
        with st.form("register_form"):
            st.subheader("S'inscrire 📝") 
            st.text_input("E-mail", key="reg_email")
            st.text_input("Mot de passe", type="password", key="reg_password")
            st.text_input("Confirmer le mot de passe", type="password", key="reg_password_confirm")
            
            st.subheader("Vos Préférences (Initiales) ⚙️") 
            
            # Niveau scolaire
            st.selectbox(
                "Niveau Scolaire (Système Marocain)",
                options=MAROC_LEVELS,
                index=len(MAROC_LEVELS) - 1, 
                key="reg_level"
            )
            
            # Langue
            st.radio(
                "Langue de Réponse",
                options=["fr", "ar"],
                format_func=lambda x: "Français 🇫🇷" if x == "fr" else "العربية 🇲🇦",
                key="reg_lang",
                horizontal=True
            )
            
            # Type de réponse
            st.selectbox(
                "Type de Réponse par Défaut",
                options=list(RESPONSE_TYPES.keys()),
                format_func=lambda x: RESPONSE_TYPES[x],
                index=0, # steps
                key="reg_response_type",
                help="Choisissez comment l'IA devrait répondre par défaut (Étapes, Concept, ou Réponse Finale)."
            )

            query_params = st.query_params
            if REFERRAL_PARAM in query_params:
                ref_email = query_params.get(REFERRAL_PARAM)
                if isinstance(ref_email, list): ref_email = ref_email[0]
                st.info(f"🔗 Vous vous inscrivez via le lien de parrainage ({ref_email}). Votre parrain recevra un bonus!") 

            st.form_submit_button("S'inscrire", type="secondary", on_click=handle_register)

        # 🌟 AJOUT DEMANDÉ : Vidéo et texte "Watch and Learn"
        st.markdown("---")
        st.markdown("🎥 **Regardez et apprenez comment utiliser l'application :**")
        try:
            st.video("https://www.youtube.com/watch?v=ZBAjwv8nu8A")
        except Exception as e:
            st.warning(f"Impossible de charger la vidéo: {e}")


# --- Fonctions de l'interface d'édition des paramètres ⚙️ --- 

def update_preference(key):
    """Met à jour une préférence utilisateur."""
    new_value = st.session_state[f'setting_{key}']
    
    st.session_state[key] = new_value 
    
    data_to_update = {key: new_value}
    
    if update_user_data(st.session_state.user_email, data_to_update):
        st.session_state.user_data[key] = new_value 
        st.sidebar.success(f"✅ Préférence mise à jour: {key}") 
    else:
        st.sidebar.error("❌ Échec de la sauvegarde. Veuillez réessayer.") 

def settings_ui():
    """Interface pour gérer les préférences dans la sidebar."""
    st.sidebar.header("🛠️ Mes Préférences (AI Output)") 
    
    current_level = st.session_state.school_level
    try:
        default_index = MAROC_LEVELS.index(current_level)
    except ValueError:
        default_index = len(MAROC_LEVELS) - 1 
        st.session_state.school_level = MAROC_LEVELS[default_index]

    # Niveau Scolaire
    st.sidebar.selectbox(
        "Niveau Scolaire (affecte la difficulté) 📚", 
        options=MAROC_LEVELS,
        index=default_index,
        key="setting_school_level",
        on_change=lambda: update_preference('school_level')
    )
    
    # Langue de Réponse
    st.sidebar.radio(
        "Langue de Réponse 🌐", 
        options=["fr", "ar"],
        format_func=lambda x: "Français 🇫🇷" if x == "fr" else "العربية 🇲🇦",
        key="setting_lang",
        index=0 if st.session_state.lang == 'fr' else 1,
        on_change=lambda: update_preference('lang'),
        horizontal=True
    )
    
    # Type de Réponse
    current_response_type = st.session_state.response_type
    try:
        default_response_index = list(RESPONSE_TYPES.keys()).index(current_response_type)
    except ValueError:
        default_response_index = 0 
        st.session_state.response_type = list(RESPONSE_TYPES.keys())[default_response_index]

    st.sidebar.selectbox(
        "Style de Réponse (affecte l'organisation) 📝", 
        options=list(RESPONSE_TYPES.keys()),
        format_func=lambda x: RESPONSE_TYPES[x],
        index=default_response_index,
        key="setting_response_type",
        on_change=lambda: update_preference('response_type'),
        help="Ceci définit la structure de l'aide fournie par l'IA."
    )
    st.sidebar.markdown("---")


def main_app_ui():
    """Interface principale de l'application (utilisateurs connectés)."""
    
    st.title("💡 Tuteur Mathématique Spécialisé (Système Marocain) 🇲🇦") 
    st.markdown("---")

    st.markdown("Je suis votre **assistant intelligent**, prêt à aider. Posez une question ou **téléchargez une image** de l'exercice.")

    col_upload, col_prompt = st.columns([1, 2])
    
    with col_upload:
        uploaded_file = st.file_uploader(
            "📷 Optionnel : Téléchargez une photo (JPG / PNG, max 4 Mo).", 
            type=["png", "jpg", "jpeg"],
            key="image_uploader"
        )
        
        if uploaded_file: 
            try:
                uploaded_file.seek(0)
                image = Image.open(BytesIO(uploaded_file.getvalue()))
                st.image(image, caption='🖼️ Image téléchargée.', use_column_width=True) 
            except Exception:
                st.error("❌ Erreur lors du chargement de l'image.") 
    
    with col_prompt:
        user_prompt = st.text_area(
            "❓ Ajoutez votre question ou votre instruction ici.", 
            height=250,
            key="prompt_input"
        )
        
        if st.button("🚀 Générer la Réponse Mathématique", use_container_width=True, type="primary"): 
            if not user_prompt and not uploaded_file:
                st.warning("☝️ Veuillez entrer une question ou télécharger une image pour commencer.") 
            else:
                with st.spinner('⏳ L\'IA analyse et prépare la réponse...'): 
                    generated_text, sources = call_gemini_api(user_prompt, uploaded_file) 
                
                st.subheader("✅ Réponse Générée :") 
                
                if generated_text and "Limite de requêtes atteinte" not in generated_text and "Échec de l'API Gemini" not in generated_text:
                    
                    st.markdown(generated_text) 
                    
                    if sources:
                        st.subheader("🌐 Sources Citées :") 
                        unique_sources = set((s['title'], s['uri']) for s in sources if s['uri'] and s['title'])
                        source_markdown = "\n".join([f"- [{title}]({uri})" for title, uri in unique_sources])
                        st.markdown(source_markdown)
                    else:
                        st.caption("ℹ️ Aucune source externe utilisée.") 
                else:
                    st.markdown(generated_text) 

    # Sidebar Status
    max_total_requests = MAX_REQUESTS + st.session_state.user_data.get('bonus_questions', 0)
    requests_left = max_total_requests - st.session_state.requests_today

    st.sidebar.header(f"👤 Statut : {st.session_state.user_email}") 
    st.sidebar.markdown(f"**Niveau Actuel:** {st.session_state.school_level}")
    st.sidebar.markdown(f"**Bonus Affiliation:** {st.session_state.user_data.get('bonus_questions', 0)} questions 🎁") 
    
    if st.session_state.user_email == ADMIN_EMAIL.lower():
        admin_dashboard_ui()
    
    settings_ui()

    if st.session_state.is_unlimited:
        status_message = "✨ **Utilisation Illimitée (VIP)**" 
        color = "#28a745"
    else:
        status_message = f"Requêtes restantes aujourd'hui: **{requests_left}** / {max_total_requests}"
        color = "#007bff" if requests_left > 0 else "#dc3545"

    st.sidebar.markdown(f"""
    <div style='background-color:#e9ecef; padding:10px; border-radius:5px; text-align:center; border-left: 5px solid {color};'>
        <span style='font-weight: bold; color: {color};'>{status_message}</span>
    </div>
    """, unsafe_allow_html=True)
    
    # 🌟 AJOUT DEMANDÉ : Informations de contact WhatsApp
    st.sidebar.markdown("---")
    st.sidebar.subheader("📞 Contactez-nous")
    st.sidebar.markdown("Pour plus d'informations :")
    # Lien WhatsApp cliquable pour une meilleure expérience utilisateur
    st.sidebar.markdown("📱 **WhatsApp :** [06 98 18 35 34](https://wa.me/212698183534)")
    
    st.sidebar.markdown("---")
    if st.sidebar.button("Déconnexion 🚪"):
        cookies[COOKIE_KEY_EMAIL] = ""
        cookies.save()
        st.session_state.auth_status = 'logged_out'
        st.session_state.should_rerun = True


# --- VII. Contrôle du
