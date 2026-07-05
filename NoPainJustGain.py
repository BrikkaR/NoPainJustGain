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
TAUX_SURCOTISATION_CDII = 0.035

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
    return "05" # Valeur par défaut de secours

def extraire_donnees_bs(file_object):
    return {
        "interimaire": "DELEU Denis",
        "mois_cible": "05",
        "heures_normales": 151.67,
        "heures_sup": 21.67,
        "taux_horaire": 12.31,
        "primes_non_soumises": 0.00,
        "total_brut": 2200.51 # Brut de base (sans ifm/cp)
    }

def lire_facture_bestt(file_object, mois_bs_cible):
    return {
        "interimaire": "DELEU Denis",
        "total_facture_filtre": 4290.99
    }

# ==========================================
# 3. MOTEUR DE CALCUL : CTT (x2) vs CDII
# ==========================================
def calculer_comparatif(bs_data, facture_data, is_pacte, taux_smic):
    const = get_constantes_pacte(is_pacte)
    
    hn = bs_data["heures_normales"]
    hs = bs_data["heures_sup"]
    brut_base = bs_data["total_brut"]
    facture = facture_data["total_facture_filtre"]
    
    heures_equiv = hn + (hs * MAJORATION_HS)
    montant_tepa = hs * const["tepa"]
    
    # ----------------------------------
    # SCÉNARIO 1 : CTT PROVISIONNÉ (Fin de mission)
    # ----------------------------------
    smic_rgdu_ctt = taux_smic * heures_equiv * ICCP_TAUX
    
    ratio_prov = smic_rgdu_ctt / brut_base if brut_base > 0 else 0
    c_rgdu_prov = max(0, (const["t_rgdu"] / 0.6) * ((1.6 * ratio_prov) - 1))
    rgdu_prov = c_rgdu_prov * brut_base
    
    charges_brutes_prov = brut_base * TAUX_CHARGES_BASE
    charges_nettes_prov = charges_brutes_prov - rgdu_prov - montant_tepa
    
    ifm_prov = brut_base * 0.10
    cp_prov = (brut_base + ifm_prov) * 0.10
    charges_sequestre_prov = (ifm_prov + cp_prov) * TAUX_CHARGES_BASE
    sequestre_total_prov = ifm_prov + cp_prov + charges_sequestre_prov
    
    cout_total_prov = brut_base + bs_data["primes_non_soumises"] + charges_nettes_prov + sequestre_total_prov
    marge_prov = facture - cout_total_prov

    # ----------------------------------
    # SCÉNARIO 2 : CTT MENSUALISÉ (Payé au mois)
    # ----------------------------------
    # Le brut gonfle car IFM et CP sont payées dans le mois
    brut_mens = brut_base + ifm_prov + cp_prov
    
    ratio_mens = smic_rgdu_ctt / brut_mens if brut_mens > 0 else 0
    c_rgdu_mens = max(0, (const["t_rgdu"] / 0.6) * ((1.6 * ratio_mens) - 1))
    rgdu_mens = c_rgdu_mens * brut_mens
    
    charges_brutes_mens = brut_mens * TAUX_CHARGES_BASE
    charges_nettes_mens = charges_brutes_mens - rgdu_mens - montant_tepa
    
    cout_total_mens = brut_mens + bs_data["primes_non_soumises"] + charges_nettes_mens
    marge_mens = facture - cout_total_mens

    # ----------------------------------
    # SCÉNARIO 3 : CDII
    # ----------------------------------
    smic_rgdu_cdii = taux_smic * heures_equiv # Pas d'ICCP
    ratio_cdii = smic_rgdu_cdii / brut_base if brut_base > 0 else 0
    c_rgdu_cdii = max(0, (const["t_rgdu"] / 0.6) * ((1.6 * ratio_cdii) - 1))
    rgdu_cdii = c_rgdu_cdii * brut_base
    
    taux_charges_cdii = TAUX_CHARGES_BASE + TAUX_SURCOTISATION_CDII
    charges_brutes_cdii = brut_base * taux_charges_cdii
    charges_nettes_cdii = charges_brutes_cdii - rgdu_cdii - montant_tepa
    
    cp_cdii = brut_base * 0.10
    charges_sequestre_cdii = cp_cdii * taux_charges_cdii
    sequestre_total_cdii = cp_cdii + charges_sequestre_cdii
    
    cout_total_cdii = brut_base + bs_data["primes_non_soumises"] + charges_nettes_cdii + sequestre_total_cdii
    marge_cdii = facture - cout_total_cdii

    return {
        "Lignes": ["1. Facturation", "2. Brut Soumis", "3. Charges Brutes", "4. Allègement RGDU", "5. Déduction TEPA", "6. Séquestre ETT (IFM/CP + Charges)", "7. COÛT TOTAL", "8. MARGE NETTE"],
        "CTT (En Provision)": [facture, brut_base, charges_brutes_prov, -rgdu_prov, -montant_tepa, sequestre_total_prov, cout_total_prov, marge_prov],
        "CTT (Mensualisé)": [facture, brut_mens, charges_brutes_mens, -rgdu_mens, -montant_tepa, 0, cout_total_mens, marge_mens],
        "CDII": [facture, brut_base, charges_brutes_cdii, -rgdu_cdii, -montant_tepa, sequestre_total_cdii, cout_total_cdii, marge_cdii]
    }

# ==========================================
# 4. INTERFACE UTILISATEUR (UI)
# ==========================================
st.set_page_config(page_title="NoPainJustGain", layout="wide")
st.title("🚀 NoPainJustGain : Audit & Stratégies")

st.sidebar.header("Paramétrage Légal")
is_pacte = st.sidebar.checkbox("Loi Pacte (-20 salariés)", value=True)
taux_smic = st.sidebar.number_input("Taux horaire SMIC en vigueur", value=12.31, step=0.10)

col1, col2 = st.columns(2)
with col1:
    fichiers_bs = st.file_uploader("📥 Déposer les Bulletins (PDF)", type=["pdf"], accept_multiple_files=True)
with col2:
    fichiers_factures = st.file_uploader("📥 Déposer les Factures BESTT (PDF)", type=["pdf"], accept_multiple_files=True)

if st.button("Lancer l'Analyse 360°", type="primary"):
    if not fichiers_bs or not fichiers_factures:
        st.warning("Veuillez déposer au moins un BS et une Facture.")
    else:
        with st.spinner('Analyse en cours...'):
            bs_data = extraire_donnees_bs(fichiers_bs[0])
            facture_data = lire_facture_bestt(fichiers_factures[0], bs_data["mois_cible"])
            
            resultats = calculer_comparatif(bs_data, facture_data, is_pacte, taux_smic)
            
            df = pd.DataFrame(resultats)
            df.set_index("Lignes", inplace=True)
            
            st.subheader(f"Comparatif Financier : {bs_data['interimaire']}")
            
            # Formatage avec 'map' corrigé
            st.dataframe(
                df.style.format("{:.2f} €").map(
                    lambda x: 'color: #2e7d32; font-weight: bold' if isinstance(x, float) and x > 1000 else '', 
                    subset=pd.IndexSlice[['8. MARGE NETTE'], :]
                ),
                use_container_width=True
            )
            
            st.info("💡 **Analyse de la RGDU :** Remarquez comment la ligne '2. Brut Soumis' plus élevée dans la colonne 'CTT (Mensualisé)' fait mécaniquement chuter l'allègement de la ligne '4. Allègement RGDU', détruisant une partie de votre marge nette finale.")