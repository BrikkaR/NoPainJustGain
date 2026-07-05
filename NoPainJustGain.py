import streamlit as st
import pandas as pd
import pdfplumber
import re
import plotly.express as px

# ==========================================
# 1. PARAMÈTRES ET CONSTANTES MÉTIER
# ==========================================
MAJORATION_HS = 1.25 # Majoration 25%
ICCP_TAUX = 1.10 # Majoration 10% CP pour base RGDU
TAUX_CHARGES_BASE = 0.45 # Estimation des charges patronales hors réductions
TAUX_SURCOTISATION_CDII = 0.035 # Surcotisation AKTO / FSPI pour le CDII

def get_constantes_pacte(is_pacte):
    """Renvoie les taux FNAL, TEPA et le paramètre T selon l'assujettissement."""
    if is_pacte:
        return {"fnal": 0.0010, "tepa": 1.50, "t_rgdu": 0.3191}
    return {"fnal": 0.0050, "tepa": 0.50, "t_rgdu": 0.3231}

# ==========================================
# 2. MOTEUR D'EXTRACTION (BATCH PROCESSING)
# ==========================================
def extraire_mois_bs(texte_bs):
    """Détecte la période du mois sur le BS."""
    match = re.search(r"Période du \d{2}/(\d{2})", texte_bs, re.IGNORECASE)
    return match.group(1) if match else "05"

def lire_factures_bestt_batch(fichiers_factures):
    """
    Parcourt toutes les factures BESTT pour extraire :
    Nom, Heures Totales travaillées et Montant Facturé.
    """
    donnees_extraites = []
    
    for fichier in fichiers_factures:
        texte_complet = ""
        with pdfplumber.open(fichier) as pdf:
            for page in pdf.pages:
                texte_complet += page.extract_text() + "\n"
        
        # Regex pour lire : "Total GLODEANU Cristian (18h00mn) => 488,27 €"
        pattern = r"Total\s+([A-Za-z\s\-]+?)\s+\(([\d]+)h([\d]+)mn\)\s*=>\s*([\d\s]+,\d{2})\s*€"
        matches = re.findall(pattern, texte_complet)
        
        for match in matches:
            nom = match[0].strip()
            heures = float(match[1])
            minutes = float(match[2])
            heures_totales_decimales = heures + (minutes / 60.0) # Convertit 18h30 en 18.5
            montant_str = match[3].replace(" ", "").replace(",", ".")
            
            donnees_extraites.append({
                "interimaire": nom,
                "heures_totales_reelles": heures_totales_decimales,
                "total_facture": float(montant_str)
            })
            
    return donnees_extraites

def simuler_bs_pour_batch(donnees_factures):
    """
    Génère un profil de Bulletin de Salaire simulé pour chaque intérimaire détecté,
    en utilisant les VRAIES heures de la facture pour valider la proratisation.
    """
    bs_batch = []
    for fact in donnees_factures:
        brut_estime = fact["total_facture"] / 1.95 # Retrouve un brut théorique
        h_totales = fact["heures_totales_reelles"]
        
        # Répartition fictive : On suppose 10% d'heures sup pour l'exemple
        hn = h_totales * 0.90
        hs = h_totales * 0.10
        
        bs_batch.append({
            "interimaire": fact["interimaire"],
            "mois_cible": "05",
            "heures_normales": hn,
            "heures_sup": hs,
            "heures_autres": 0.0, # Nuit, dimanche, etc.
            "taux_horaire": 12.31,
            "primes_non_soumises": 0.00,
            "total_brut": brut_estime
        })
    return bs_batch

# ==========================================
# 3. MOTEUR DE CALCUL : CTT vs CDII (AVEC PRORATISATION & PLAFONDS)
# ==========================================
def calculer_comparatif(bs_data, facture_data, is_pacte, taux_smic):
    const = get_constantes_pacte(is_pacte)
    
    # 1. PRORATISATION DES HEURES
    hn = bs_data.get("heures_normales", 0.0)
    hs = bs_data.get("heures_sup", 0.0)
    hautres = bs_data.get("heures_autres", 0.0)
    
    brut_base = bs_data["total_brut"]
    facture = facture_data["total_facture"]
    
    # Le temps de travail équivalent (pour le SMIC de référence)
    heures_equiv = hn + hautres + (hs * MAJORATION_HS)
    montant_tepa = hs * const["tepa"]
    
    # ----------------------------------
    # SCÉNARIO 1 : CTT PROVISIONNÉ (Fin de mission)
    # ----------------------------------
    smic_rgdu_ctt = taux_smic * heures_equiv * ICCP_TAUX # Inclus 10% CP
    ratio_prov = smic_rgdu_ctt / brut_base if brut_base > 0 else 0
    
    # CALCUL SÉCURISÉ : Ne peut pas dépasser le plafond légal (const["t_rgdu"])
    c_rgdu_prov_calcul = (const["t_rgdu"] / 0.6) * ((1.6 * ratio_prov) - 1)
    c_rgdu_prov = min(const["t_rgdu"], max(0, c_rgdu_prov_calcul))
    rgdu_prov = c_rgdu_prov * brut_base
    
    charges_nettes_prov = (brut_base * TAUX_CHARGES_BASE) - rgdu_prov - montant_tepa
    ifm_prov = brut_base * 0.10
    cp_prov = (brut_base + ifm_prov) * 0.10
    sequestre_total_prov = (ifm_prov + cp_prov) * (1 + TAUX_CHARGES_BASE)
    cout_total_prov = brut_base + bs_data["primes_non_soumises"] + charges_nettes_prov + sequestre_total_prov
    marge_prov = facture - cout_total_prov

    # ----------------------------------
    # SCÉNARIO 2 : CTT MENSUALISÉ (Payé au mois)
    # ----------------------------------
    brut_mens = brut_base + ifm_prov + cp_prov # Le brut explose
    ratio_mens = smic_rgdu_ctt / brut_mens if brut_mens > 0 else 0
    
    # CALCUL SÉCURISÉ
    c_rgdu_mens_calcul = (const["t_rgdu"] / 0.6) * ((1.6 * ratio_mens) - 1)
    c_rgdu_mens = min(const["t_rgdu"], max(0, c_rgdu_mens_calcul))
    rgdu_mens = c_rgdu_mens * brut_mens
    
    charges_nettes_mens = (brut_mens * TAUX_CHARGES_BASE) - rgdu_mens - montant_tepa
    cout_total_mens = brut_mens + bs_data["primes_non_soumises"] + charges_nettes_mens
    marge_mens = facture - cout_total_mens

    # ----------------------------------
    # SCÉNARIO 3 : CDII (Pas d'IFM, Pas d'ICCP sur le SMIC)
    # ----------------------------------
    smic_rgdu_cdii = taux_smic * heures_equiv
    ratio_cdii = smic_rgdu_cdii / brut_base if brut_base > 0 else 0
    
    # CALCUL SÉCURISÉ
    c_rgdu_cdii_calcul = (const["t_rgdu"] / 0.6) * ((1.6 * ratio_cdii) - 1)
    c_rgdu_cdii = min(const["t_rgdu"], max(0, c_rgdu_cdii_calcul))
    rgdu_cdii = c_rgdu_cdii * brut_base
    
    charges_nettes_cdii = (brut_base * (TAUX_CHARGES_BASE + TAUX_SURCOTISATION_CDII)) - rgdu_cdii - montant_tepa
    cp_cdii = brut_base * 0.10
    sequestre_total_cdii = cp_cdii * (1 + TAUX_CHARGES_BASE + TAUX_SURCOTISATION_CDII)
    cout_total_cdii = brut_base + bs_data["primes_non_soumises"] + charges_nettes_cdii + sequestre_total_cdii
    marge_cdii = facture - cout_total_cdii

    return {
        "Interimaire": bs_data["interimaire"],
        "Heures": round(hn + hs + hautres, 2),
        "Data": {
            "Lignes": ["1. Facturation HT", "2. Brut Soumis", "3. Allègement RGDU", "4. Séquestre ETT (Primes+Charges)", "5. COÛT TOTAL", "6. MARGE NETTE"],
            "CTT (Provision)": [facture, brut_base, -rgdu_prov, sequestre_total_prov, cout_total_prov, marge_prov],
            "CTT (Mensualisé)": [facture, brut_mens, -rgdu_mens, 0.00, cout_total_mens, marge_mens],
            "CDII": [facture, brut_base, -rgdu_cdii, sequestre_total_cdii, cout_total_cdii, marge_cdii]
        }
    }

# ==========================================
# 4. INTERFACE UTILISATEUR & VUES (UI)
# ==========================================
st.set_page_config(page_title="NoPainJustGain", layout="wide")
st.title("🚀 NoPainJustGain : Contrôle de Gestion & Stratégies")

st.sidebar.header("Paramétrage Légal")
is_pacte = st.sidebar.checkbox("Loi Pacte (-20 salariés)", value=True)
taux_smic = st.sidebar.number_input("Taux horaire SMIC (ex: 12.02 ou 12.31)", value=12.02, step=0.01)

col1, col2 = st.columns(2)
with col1:
    fichiers_bs = st.file_uploader("📥 Déposer les Bulletins (PDF)", type=["pdf"], accept_multiple_files=True)
with col2:
    fichiers_factures = st.file_uploader("📥 Déposer les Factures BESTT (PDF)", type=["pdf"], accept_multiple_files=True)

if st.button("Lancer l'Audit Automatique", type="primary"):
    if not fichiers_bs or not fichiers_factures:
        st.warning("Veuillez déposer des documents pour démarrer l'analyse.")
    else:
        with st.spinner('Extraction et calcul en cours...'):
            factures_batch = lire_factures_bestt_batch(fichiers_factures)
            bs_batch = simuler_bs_pour_batch(factures_batch)
            
            master_results = []
            for i in range(len(factures_batch)):
                res = calculer_comparatif(bs_batch[i], factures_batch[i], is_pacte, taux_smic)
                master_results.append(res)

        st.success(f"Audit terminé ! {len(master_results)} intérimaires détectés.")
        st.markdown("---")

        # ----------------------------------
        # VUE 1 : GRAPHIQUE GLOBAL (PLOTLY)
        # ----------------------------------
        st.subheader("📊 Comparatif Visuel des Marges Nettes")
        
        graph_data = []
        for r in master_results:
            df_temp = pd.DataFrame(r["Data"]).set_index("Lignes")
            nom = f"{r['Interimaire']} ({r['Heures']}h)"
            graph_data.append({"Intérimaire": nom, "Contrat": "CTT (Provision)", "Marge": df_temp.loc["6. MARGE NETTE", "CTT (Provision)"]})
            graph_data.append({"Intérimaire": nom, "Contrat": "CTT (Mensualisé)", "Marge": df_temp.loc["6. MARGE NETTE", "CTT (Mensualisé)"]})
            graph_data.append({"Intérimaire": nom, "Contrat": "CDII", "Marge": df_temp.loc["6. MARGE NETTE", "CDII"]})
            
        df_graph = pd.DataFrame(graph_data)
        fig = px.bar(df_graph, x="Intérimaire", y="Marge", color="Contrat", barmode="group",
                     color_discrete_map={"CTT (Provision)": "#1E88E5", "CTT (Mensualisé)": "#E53935", "CDII": "#43A047"})
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("---")
        st.subheader("📑 Détails Ligne à Ligne (Codes Couleurs)")
        
        # ----------------------------------
        # VUE 2 : TABLEAUX DÉTAILLÉS PAR SALARIÉ
        # ----------------------------------
        for r in master_results:
            with st.expander(f"Dossier : {r['Interimaire']} - Temps de travail : {r['Heures']}h", expanded=True):
                df = pd.DataFrame(r["Data"]).set_index("Lignes")
                
                def style_dataframe(row):
                    """Applique les couleurs : Vert pour le meilleur, Rouge pour le pire."""
                    styles = [''] * len(row)
                    
                    if row.name == "6. MARGE NETTE":
                        is_max = row == row.max()
                        is_min = row == row.min()
                        return ['background-color: #d4edda; color: #155724; font-weight: bold' if v 
                                else 'background-color: #f8d7da; color: #721c24' if m 
                                else 'font-weight: bold' for v, m in zip(is_max, is_min)]
                    
                    if row.name == "3. Allègement RGDU":
                        # Le chiffre le plus proche de zéro (le plus grand mathématiquement car négatif) est la pire perte
                        is_worst_rgdu = row == row.max() 
                        return ['color: #721c24; font-weight: bold' if w else 'color: #155724' for w in is_worst_rgdu]
                    
                    return styles

                st.dataframe(
                    df.style.format("{:.2f} €").apply(style_dataframe, axis=1),
                    use_container_width=True
                )