import streamlit as st
import pandas as pd
import pdfplumber
import re

# ==========================================
# 1. PARAMÈTRES ET CONSTANTES MÉTIER
# ==========================================
MAJORATION_HS = 1.25
ICCP_TAUX = 1.10
TAUX_CHARGES_BASE = 0.45
TAUX_SURCOTISATION_CDII = 0.035 # Majoration AKTO / FSPI pour CDII (estimé à 3.5%)

def get_constantes_pacte(is_pacte):
    if is_pacte:
        return {"fnal": 0.0010, "tepa": 1.50, "t_rgdu": 0.3191}
    return {"fnal": 0.0050, "tepa": 0.50, "t_rgdu": 0.3231}

# ==========================================
# 2. MOTEUR D'EXTRACTION 
# ==========================================
def extraire_mois_bs(texte_bs):
    match = re.search(r"Période du \d{2}/(\d{2})", texte_bs, re.IGNORECASE)
    if match:
        return match.group(1)
    return None

def extraire_donnees_bs(file_object):
    # Simulation d'extraction pour le MVP
    return {
        "interimaire": "DELEU Denis",
        "mois_cible": "05",
        "heures_normales": 151.67,
        "heures_sup": 21.67,
        "taux_horaire": 12.31,
        "primes_non_soumises": 0.00,
        "total_brut": 2200.51
    }

def lire_facture_bestt(file_object, mois_bs_cible):
    # Simulation d'extraction filtrée pour le MVP
    return {
        "interimaire": "DELEU Denis",
        "total_facture_filtre": 4290.99
    }

# ==========================================
# 3. MOTEUR DE CALCUL : SYMBIOSE CTT vs CDII
# ==========================================
def calculer_comparatif(bs_data, facture_data, is_pacte, taux_smic):
    const = get_constantes_pacte(is_pacte)
    
    hn = bs_data["heures_normales"]
    hs = bs_data["heures_sup"]
    brut = bs_data["total_brut"]
    facture = facture_data["total_facture_filtre"]
    
    heures_equiv = hn + (hs * MAJORATION_HS)
    montant_tepa = hs * const["tepa"]
    
    # ----------------------------------
    # BRANCHE 1 : CALCUL CTT (Classique)
    # ----------------------------------
    smic_rgdu_ctt = taux_smic * heures_equiv * ICCP_TAUX # Majoration 10%
    ratio_ctt = smic_rgdu_ctt / brut if brut > 0 else 0
    c_rgdu_ctt = max(0, (const["t_rgdu"] / 0.6) * ((1.6 * ratio_ctt) - 1))
    rgdu_ctt = c_rgdu_ctt * brut
    
    charges_brutes_ctt = brut * TAUX_CHARGES_BASE
    charges_nettes_ctt = charges_brutes_ctt - rgdu_ctt - montant_tepa
    
    ifm_ctt = brut * 0.10
    cp_ctt = (brut + ifm_ctt) * 0.10
    charges_sequestre_ctt = (ifm_ctt + cp_ctt) * TAUX_CHARGES_BASE
    sequestre_total_ctt = ifm_ctt + cp_ctt + charges_sequestre_ctt
    
    cout_patronal_ctt = brut + bs_data["primes_non_soumises"] + charges_nettes_ctt + sequestre_total_ctt
    marge_ctt = facture - cout_patronal_ctt

    # ----------------------------------
    # BRANCHE 2 : CALCUL CDII
    # ----------------------------------
    smic_rgdu_cdii = taux_smic * heures_equiv # PAS de majoration 10%
    ratio_cdii = smic_rgdu_cdii / brut if brut > 0 else 0
    c_rgdu_cdii = max(0, (const["t_rgdu"] / 0.6) * ((1.6 * ratio_cdii) - 1))
    rgdu_cdii = c_rgdu_cdii * brut
    
    taux_charges_cdii = TAUX_CHARGES_BASE + TAUX_SURCOTISATION_CDII
    charges_brutes_cdii = brut * taux_charges_cdii
    charges_nettes_cdii = charges_brutes_cdii - rgdu_cdii - montant_tepa
    
    ifm_cdii = 0 # Pas d'IFM
    cp_cdii = brut * 0.10
    charges_sequestre_cdii = cp_cdii * taux_charges_cdii
    sequestre_total_cdii = cp_cdii + charges_sequestre_cdii
    
    cout_patronal_cdii = brut + bs_data["primes_non_soumises"] + charges_nettes_cdii + sequestre_total_cdii
    marge_cdii = facture - cout_patronal_cdii

    # ----------------------------------
    # ANALYSE DU RISQUE (INTER-MISSION)
    # ----------------------------------
    gain_cdii = marge_cdii - marge_ctt
    
    # Coût estimé d'une journée à rien faire (7h au SMIC + charges pleines)
    cout_journee_inactive = (7 * taux_smic) * (1 + taux_charges_cdii)
    
    point_mort_jours = gain_cdii / cout_journee_inactive if gain_cdii > 0 else 0

    return {
        "Data": {
            "Lignes": ["Facturation", "Brut", "Charges Brutes", "RGDU", "TEPA", "Charges Nettes", "Séquestre ETT", "Coût Total", "MARGE NETTE"],
            "CTT": [facture, brut, charges_brutes_ctt, -rgdu_ctt, -montant_tepa, charges_nettes_ctt, sequestre_total_ctt, cout_patronal_ctt, marge_ctt],
            "CDII": [facture, brut, charges_brutes_cdii, -rgdu_cdii, -montant_tepa, charges_nettes_cdii, sequestre_total_cdii, cout_patronal_cdii, marge_cdii]
        },
        "Insights": {
            "Gain CDII": gain_cdii,
            "Point Mort": point_mort_jours
        }
    }

# ==========================================
# 4. INTERFACE UTILISATEUR (UI)
# ==========================================
st.set_page_config(page_title="NoPainJustGain", layout="wide")
st.title("🚀 NoPainJustGain : Audit de Marge & Stratégie CDII")

st.sidebar.header("Paramétrage Légal")
is_pacte = st.sidebar.checkbox("Loi Pacte (-20 salariés)", value=True)
taux_smic = st.sidebar.number_input("Taux horaire SMIC en vigueur", value=11.65, step=0.10)

col1, col2 = st.columns(2)
with col1:
    fichiers_bs = st.file_uploader("📥 Déposer les Bulletins (PDF)", type=["pdf"], accept_multiple_files=True)
with col2:
    fichiers_factures = st.file_uploader("📥 Déposer les Factures BESTT (PDF)", type=["pdf"], accept_multiple_files=True)

if st.button("Lancer l'Analyse Symbiotique", type="primary"):
    if not fichiers_bs or not fichiers_factures:
        st.warning("Veuillez déposer au moins un BS et une Facture.")
    else:
        with st.spinner('Analyse en cours...'):
            bs_data = extraire_donnees_bs(fichiers_bs[0])
            facture_data = lire_facture_bestt(fichiers_factures[0], bs_data["mois_cible"])
            
            resultat = calculer_comparatif(bs_data, facture_data, is_pacte, taux_smic)
            
            df = pd.DataFrame(resultat["Data"])
            df.set_index("Lignes", inplace=True)
            
            # Calcul de l'écart
            df["Écart (CDII - CTT)"] = df["CDII"] - df["CTT"]
            
            st.subheader(f"Analyse pour l'intérimaire : {bs_data['interimaire']}")
            
            # Formatage du tableau
            st.dataframe(
                df.style.format("{:.2f} €").applymap(
                    lambda x: 'color: green' if x > 0 else 'color: red' if x < 0 else '', 
                    subset=['Écart (CDII - CTT)']
                ),
                use_container_width=True
            )
            
            # Zone d'aide à la décision
            gain = resultat["Insights"]["Gain CDII"]
            jours_limite = resultat["Insights"]["Point Mort"]
            
            st.markdown("---")
            st.subheader("💡 Aide à la décision (Risque d'Inter-mission)")
            
            if gain > 0:
                st.success(f"**Le CDII est plus rentable de {gain:.2f} € sur ce mois travaillé.**")
                st.warning(f"⚠️ **Attention :** Cette avance de trésorerie sera totalement détruite si l'intérimaire passe **{jours_limite:.1f} jours en inter-mission** dans le mois. Au-delà, l'agence perd de l'argent par rapport à un contrat CTT classique.")
            else:
                st.error(f"**Le CTT classique reste plus rentable de {abs(gain):.2f} €.** Le profil de paie (heures/primes) écrase les avantages du CDII.")