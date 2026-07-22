import streamlit as st
import pandas as pd
import pdfplumber
import re
import plotly.express as px

# ==========================================
# 1. PARAMÈTRES ET CONSTANTES MÉTIER
# ==========================================
# Majoration légale des heures supp CÔTÉ PAIE (25 %). Informative : le brut est lu
# sur le bulletin, la majoration réellement payée y est déjà incluse (elle peut varier
# de 110 à 120 % pour certains clients avec complément salarial). NON utilisée pour le
# SMIC de référence de l'allègement, qui retient les heures supp à ×1,00 (art. D. 241-7).
MAJORATION_HS = 1.25
TAUX_CHARGES_BASE = 0.45          # taux patronal moyen AVANT allègement (approximation paramétrable)
TAUX_SURCOTISATION_CDII = 0.035  # surcotisation patronale CDII (AKTO/FSPI), estimation

# Majoration « caisse de congés payés » (intérim/BTP) : appliquée AU COEFFICIENT.
# En paie c'est la « majoration de 10 % », techniquement ×100/90 (reproduit les
# chiffres officiels type BTP). Mettre 1.10 ici pour un +10 % strict si souhaité.
MAJORATION_ICCP = 100 / 90

# --- RÉGIME PAR DATE : Fillon (< 01/01/2026) vs RGDU (>= 01/01/2026) -----------
# RGDU 2026 (décret n°2025-887 modifié par 2025-1446 ; gel SMIC réf. par 2026-509
# du 12/06/2026) : C = Tmin + Tdelta × [½ × (3 × SMICréf / RAB − 1)]^1,75, plafonné
# à Tmax (= Tmin + Tdelta), plancher de 2 % jusqu'à 3 SMIC puis 0 au-delà.
RGDU_TMIN = 0.0200
RGDU_EXPOSANT = 1.75
RGDU_TDELTA = {True: 0.3781, False: 0.3821}   # clé = FNAL réduit (effectif < 50)
RGDU_TMAX = {True: 0.3981, False: 0.4021}
RGDU_SEUIL_SORTIE = 3.0                        # sortie à 3 SMIC

# SMIC de référence de l'allègement (€/h), gelé sur l'année à sa valeur du 1er janvier.
# ⚠️ À maintenir chaque année (le SMIC change 1 à 2 fois/an ; seule la valeur du
# 1er janvier compte pour l'allègement — la hausse de juin 2026 à 12,31 € n'est PAS
# répercutée). Valeurs pré-2026 = SMIC applicable (Fillon n'était pas gelé).
SMIC_REF_ANNEE = {2026: 12.02, 2025: 11.88, 2024: 11.65}

# Coefficient T maximal de la réduction Fillon (pré-2026), par (année, FNAL réduit).
# ⚠️ VALEURS À VÉRIFIER selon l'année auditée avant toute exploitation commerciale.
T_FILLON = {
    (2025, True): 0.3193, (2025, False): 0.3233,
    (2024, True): 0.3194, (2024, False): 0.3234,
    (2023, True): 0.3191, (2023, False): 0.3231,
}

def parametres_regime(annee, effectif, smic_ref_override=None, t_fillon_override=None):
    """
    Sélectionne le régime d'allègement selon la DATE de prestation et l'EFFECTIF.
    - Régime : Fillon si année < 2026, RGDU si année >= 2026.
    - Seuils d'effectif DISTINCTS : FNAL/RGDU à 50 salariés, TEPA à 20 salariés.
    Renvoie un dict de paramètres consommé par calcul_allegement().
    """
    fnal_reduit = effectif < 50                     # FNAL 0,10 % (<50) sinon 0,50 %
    tepa = 1.50 if effectif < 20 else 0.50          # déduction TEPA/h : seuil 20 salariés
    smic_ref = smic_ref_override if smic_ref_override else SMIC_REF_ANNEE.get(annee, 12.02)

    if annee >= 2026:
        return {
            "regime": "RGDU", "tepa": tepa, "fnal_reduit": fnal_reduit,
            "smic_ref_horaire": smic_ref, "seuil_sortie": RGDU_SEUIL_SORTIE,
            "tmin": RGDU_TMIN, "exposant": RGDU_EXPOSANT,
            "tdelta": RGDU_TDELTA[fnal_reduit], "tmax": RGDU_TMAX[fnal_reduit],
        }
    t_max = t_fillon_override if t_fillon_override else \
        T_FILLON.get((annee, fnal_reduit), 0.3191 if fnal_reduit else 0.3231)
    return {
        "regime": "Fillon", "tepa": tepa, "fnal_reduit": fnal_reduit,
        "smic_ref_horaire": smic_ref, "seuil_sortie": 1.6, "t_max": t_max,
    }

def calcul_allegement(params, smic_ref_mois, brut_ref, majoration=1.0):
    """
    Montant d'allègement (Fillon ou RGDU) sur base mensuelle (proxy de l'annuel).
    smic_ref_mois : SMIC de référence proratisé aux heures du mois.
    majoration    : ×100/90 (caisse CP) pour les CTT ; 1.0 pour le CDII.
    Renvoie (coefficient, montant).
    ⚠️ Proxy MENSUEL : l'allègement légal se calcule sur la rémunération ANNUELLE
    avec régularisation. Écart possible en intérim (contrats fragmentés), surtout
    près du point de sortie.
    """
    if brut_ref <= 0:
        return 0.0, 0.0
    # Sortie franche : 1,6 SMIC (Fillon) ou 3 SMIC (RGDU)
    if brut_ref >= params["seuil_sortie"] * smic_ref_mois:
        return 0.0, 0.0
    ratio = smic_ref_mois / brut_ref
    if params["regime"] == "RGDU":
        x = max(0.0, 0.5 * (3.0 * ratio - 1.0))
        c = params["tmin"] + params["tdelta"] * (x ** params["exposant"])
        c = min(params["tmax"], c)               # plafond Tmax (= Tmin + Tdelta)
    else:  # Fillon
        c = (params["t_max"] / 0.6) * ((1.6 * ratio) - 1.0)
        c = min(params["t_max"], max(0.0, c))
    c *= majoration                              # majoration caisse CP (coefficient)
    return round(c, 4), c * brut_ref

MOIS_LABELS = {
    "01": "Janvier", "02": "Février", "03": "Mars", "04": "Avril",
    "05": "Mai", "06": "Juin", "07": "Juillet", "08": "Août",
    "09": "Septembre", "10": "Octobre", "11": "Novembre", "12": "Décembre",
}

# ==========================================
# 2. OUTILS DE PARSING GÉNÉRIQUES
# ==========================================
# Nombre au format FR, signe négatif optionnel (lignes de régularisation) et
# 2 à 3 décimales (colonnes IFM/CP des bulletins affichent 3 décimales).
_MONTANT_FR = r"-?\d{1,3}(?:[ \u00a0\u202f.]\d{3})*,\d{2,3}|-?\d+,\d{2,3}"

def parse_montant_fr(s):
    """Convertit un montant FR ('3 170,11' / '3.170,11' / '2393,78') en float."""
    if s is None:
        return 0.0
    s = s.strip()
    for ch in ("\u202f", "\u00a0", " "):
        s = s.replace(ch, "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")  # virgule décimale -> points = milliers
    try:
        return float(s)
    except ValueError:
        return 0.0

def _tous_les_montants(ligne):
    return [parse_montant_fr(m) for m in re.findall(_MONTANT_FR, ligne)]

# ==========================================
# 3. MOTEUR D'EXTRACTION FACTURES (BESTT)
# ==========================================
# Nom d'intérimaire en début de ligne suivi d'un intitulé de poste entre parenthèses.
# Accepte les accents et n'importe quel intitulé (pas seulement "AGENT").
_RE_NOM = re.compile(r"^([A-ZÀ-Ÿ][A-ZÀ-Ÿ'\-]+(?:\s+[A-Za-zÀ-ÿ'\-]+)+?)\s*\(", re.UNICODE)
# Toutes les dates jj/mm (avec éventuellement /aa ou /aaaa)
_RE_DATES = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/\d{2,4})?\b")
# Rubriques NON soumises à cotisations (remboursements de frais -> refacturés au coef 1,00)
_RE_NON_SOUMISE = re.compile(r"PANIER|TICKET|RESTAUR|TRANSPORT|REMBOURS|D[ÉE]PLACEMENT|KILOM", re.I)

def _libelle_facture(ligne):
    """Libellé d'une ligne de facture : texte entre la parenthèse de date et le 1er
    nombre décimal (pas le 1er chiffre, sinon on tronque « HEURES SUPP. 25 % »)."""
    m = re.search(r"\)\s*(.+?)\s+-?\d[\d\u00a0 ]*,\d", ligne)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""

def mois_de_la_ligne(ligne):
    """
    Renvoie (mois_attribution, liste_mois_trouves).
    Règle : mois d'attribution d'une semaine = mois de la DATE DE FIN
    (dernière date jj/mm de la ligne), reflétant la prestation facturée.
    """
    dates = _RE_DATES.findall(ligne)
    if not dates:
        return None, []
    mois_trouves = [mm.zfill(2) for (_jj, mm) in dates]
    return mois_trouves[-1], mois_trouves

def est_ligne_prestation(ligne):
    """
    Ligne de prestation BESTT = repère de semaine 'Sem.NN' + au moins une date.
    Le repère 'Sem' est requis pour exclure les en-têtes/pieds de page répétés
    (ex. « Période du 01/05/2026 au 21/06/2026 ») qui contiennent aussi des dates.
    """
    return bool(re.search(r"sem\.?\s*\d", ligne, re.IGNORECASE)) and bool(_RE_DATES.search(ligne))

def parser_lignes_facture(lignes, nom_fichier, mois_cible, consolidation):
    """Parse les lignes d'une page de facture et alimente `consolidation` (dict mutable)."""
    interimaire = None
    for ligne in lignes:
        if not ligne or not ligne.strip():
            continue

        m_nom = _RE_NOM.search(ligne)
        if m_nom and not _RE_DATES.search(ligne):
            interimaire = re.sub(r"\s+", " ", m_nom.group(1)).strip()
            consolidation.setdefault(interimaire, {"total": 0.0, "historique_lignes": []})
            continue

        if interimaire and est_ligne_prestation(ligne):
            mois_attr, mois_trouves = mois_de_la_ligne(ligne)
            if mois_attr is None:
                continue
            # Seules les lignes avec un total facturé « = … € » sont facturées.
            # Les lignes sans « = » (prime de référence, journée de solidarité non
            # facturée) affichent un taux mais aucun montant -> à ne PAS sommer,
            # sinon le total diverge du total imprimé sur la facture.
            a_total = "=" in ligne
            montants = _tous_les_montants(ligne)
            # Montant facturé = 1er montant APRÈS le dernier « = » (et non le dernier
            # nombre de la ligne : le format Puget ajoute une colonne « Coef » 1,9000
            # en fin de ligne qui serait sinon prise pour le total).
            montant = 0.0
            if a_total:
                apres_egal = ligne.rsplit("=", 1)[1]
                ms_ap = _tous_les_montants(apres_egal)
                montant = ms_ap[0] if ms_ap else 0.0
            retenue = (mois_attr == mois_cible) and a_total

            # Métadonnées pour le contrôle des coefficients de facturation.
            libelle = _libelle_facture(ligne)
            # Qté et taux facturé via l'ancre « <qté> x <taux> » (robuste aux deux
            # formats : Nice « 13,50 x 23,75 € », Puget « 12,50 € 14,00 x 23,750 € »).
            qte = taux_fact = None
            mx = re.search(r"(-?\d[\d\u00a0 .]*,\d+)\s*[x×]\s*(-?\d[\d\u00a0 .]*,\d+)", ligne)
            if mx:
                qte = parse_montant_fr(mx.group(1))
                taux_fact = parse_montant_fr(mx.group(2))
            elif qte not in (None, 0) and a_total and montant:
                taux_fact = round(montant / qte, 4)
            soumise = not bool(_RE_NON_SOUMISE.search(libelle))

            if not a_total:
                statut = "ℹ️ non facturée (sans total « = »)"
            elif retenue:
                statut = "✅ RETENUE"
            else:
                statut = f"⏭️ ignorée (mois {mois_attr})"
            consolidation[interimaire]["historique_lignes"].append({
                "fichier": nom_fichier, "ligne": ligne.strip(),
                "mois_attribution": mois_attr, "mois_trouves": mois_trouves,
                "montant": montant, "retenue": retenue, "statut": statut,
                "libelle": libelle, "qte": qte, "taux_fact": taux_fact, "soumise": soumise,
            })
            if retenue:
                consolidation[interimaire]["total"] += montant

def lire_factures_bestt_consolidees(fichiers_factures, mois_cible="05"):
    consolidation = {}
    for fichier in fichiers_factures:
        # On concatène TOUTES les pages avant de parser : ainsi l'en-tête intérimaire
        # persiste quand ses lignes se poursuivent d'une page à l'autre (ex. SIMION,
        # dont le bloc s'étale sur 2 pages de la facture 369).
        texte_complet = ""
        with pdfplumber.open(fichier) as pdf:
            for page in pdf.pages:
                texte_complet += (page.extract_text() or "") + "\n"
        parser_lignes_facture(texte_complet.split("\n"), fichier.name, mois_cible, consolidation)

    donnees = []
    for nom, data in consolidation.items():
        nb_retenues = sum(1 for l in data["historique_lignes"] if l["retenue"])
        if nb_retenues > 0:   # au moins une ligne du mois cible (même si total net = 0 -> anomalie)
            donnees.append({
                "interimaire": nom,
                "total_facture": round(data["total"], 2),
                "lignes_retenues": data["historique_lignes"],
            })
    return donnees

# ==========================================
# 4. MOTEUR D'EXTRACTION BULLETINS DE SALAIRE
# ==========================================
_LABELS_HEURES = [
    r"Heures?\s+normales?", r"Temps\s+de\s+travail", r"Salaire\s+de\s+base", r"\bBase\b",
]

def grouper_lignes_depuis_mots(mots, tol=3.0):
    """
    Reconstruit les lignes VISUELLES d'une page à partir des mots positionnés
    (pdfplumber extract_words). Indispensable pour les bulletins en colonnes :
    le libellé 'SB' et sa valeur, à droite, se retrouvent sur la même ligne,
    et le montant '2 393,78' (fragmenté par l'espace des milliers) est reformé.
    """
    rows = []
    for w in sorted(mots, key=lambda m: (m["top"], m["x0"])):
        placed = False
        for r in rows:
            if abs(r["top"] - w["top"]) <= tol:
                r["mots"].append(w)
                r["top"] = (r["top"] * (len(r["mots"]) - 1) + w["top"]) / len(r["mots"])
                placed = True
                break
        if not placed:
            rows.append({"top": w["top"], "mots": [w]})
    lignes = []
    for r in sorted(rows, key=lambda r: r["top"]):
        r["mots"].sort(key=lambda m: m["x0"])
        lignes.append(" ".join(m["text"] for m in r["mots"]))
    return lignes

# SB (colonne « = BRUT ») = base + IFM(10%) + CP(10% sur base+IFM) = 1,21 × base.
# On dérive donc la base soumise à partir du SB lu.
RATIO_SB_BASE = 1.21
_RE_SB = re.compile(r"(?<![A-Za-zÀ-ÿ0-9])S\.?\s?B\.?(?![A-Za-zÀ-ÿ0-9])")

def brut_depuis_rows(rows):
    """
    Extrait le brut social depuis des lignes visuelles reconstruites.
    Structure BESTT/paie : ligne TOTAUX du bloc « SOUMISES À COTISATIONS » avec les
    colonnes MONTANT | +IFM | +CP | = BRUT, et le code « SB » en marge de DROITE,
    juste APRÈS la valeur du brut.
    Retourne (sb, base, methode, ligne_source) ; (None, None, None, None) si rien
    (à distinguer d'un brut réellement égal à 0).
    """
    def montants(s):
        return [parse_montant_fr(x) for x in re.findall(_MONTANT_FR, s)]

    # 1) Code « SB » : le BRUT est le DERNIER montant AVANT le code SB (marge de droite).
    for r in rows:
        m = _RE_SB.search(r)
        if m:
            avant = montants(r[:m.start()])
            if avant:
                sb = avant[-1]
                base = sb / RATIO_SB_BASE
                # Base exacte = colonne MONTANT = 1er montant APRÈS le repère d'heures « Xh ».
                # Vérifie MONTANT (+IFM) (+CP) = BRUT. Gère les 3 cas : IFM+CP (÷1,21),
                # IFM seul ou CP seul (÷1,10) — ex. MARIAN, CP sans IFM.
                mh = re.search(r"\d[\d\u00a0 .]*,\d+\s*h", r[:m.start()])
                apres = montants(r[mh.end():m.start()]) if mh else []
                if (len(apres) >= 2 and apres[-1] > 0
                        and abs(sum(apres[:-1]) - apres[-1]) <= max(0.05, 0.01 * apres[-1])):
                    base, sb = apres[0], apres[-1]
                elif len(avant) >= 4:   # repli : 4 colonnes cohérentes
                    b, i, c, br = avant[-4], avant[-3], avant[-2], avant[-1]
                    if br > 0 and abs((b + i + c) - br) <= max(0.05, 0.01 * br):
                        base, sb = b, br
                return sb, base, "SB (colonne « = BRUT »)", r

    # 2) Libellés explicites -> montant = SB, base dérivée
    libelles = [
        (r"salaire\s+brut(?:\s+imposable)?", "Salaire brut"),
        (r"brut\s+social", "Brut social"),
        (r"total\s+brut", "Total brut"),
        (r"brut\s+total", "Brut total"),
        (r"r[ée]mun[ée]ration\s+brute", "Rémunération brute"),
        (r"brut\s+fiscal", "Brut fiscal"),
    ]
    for pattern, name in libelles:
        rx = re.compile(pattern, re.IGNORECASE)
        for r in rows:
            if rx.search(r):
                ms = montants(r)
                if ms:
                    sb = ms[-1]
                    return sb, sb / RATIO_SB_BASE, name, r

    # 3) 'brut' seul -> dernier montant
    for r in rows:
        if re.search(r"\bbrut\b", r, re.IGNORECASE):
            ms = montants(r)
            if ms:
                sb = ms[-1]
                return sb, sb / RATIO_SB_BASE, "Brut", r

    return None, None, None, None

def extraire_brut(texte, mots=None):
    """
    Retourne (sb, base, methode, ligne_source, lignes_visuelles).
    sb = brut social (colonne = BRUT, inclut IFM/CP) ; base = sb / 1,21 (base soumise).
    sb = None si aucune ligne de brut n'est identifiée (≠ brut = 0).
    """
    if mots:
        rows = grouper_lignes_depuis_mots(mots)
        sb, base, methode, ligne = brut_depuis_rows(rows)
        if sb is not None:
            return sb, base, methode, ligne, rows
        sb2, base2, methode2, ligne2 = brut_depuis_rows(texte.split("\n"))
        return sb2, base2, methode2, ligne2, rows
    rows = texte.split("\n")
    sb, base, methode, ligne = brut_depuis_rows(rows)
    return sb, base, methode, ligne, rows

def extraire_heures(texte, rows=None, defaut=151.67):
    """Heures totales travaillées : priorité à la ligne TOTAUX « HEURES : X h »."""
    candidats = rows if rows else texte.split("\n")
    # 1) Total explicite « HEURES : 199,15 h » (ligne TOTAUX du bloc soumis)
    for r in candidats:
        m = re.search(r"HEURES\s*:?\s*(\d{1,3}(?:[.,]\d{1,2})?)\s*h", r, re.IGNORECASE)
        if m:
            g = m.group(1)
            val = parse_montant_fr(g) if "," in g else float(g)
            if 1 <= val <= 400:
                return val
    # 2) Repli : libellés d'heures classiques
    for label in _LABELS_HEURES:
        rx = re.compile(label, re.IGNORECASE)
        for ligne in candidats:
            if rx.search(ligne):
                for mm in re.findall(r"\d{1,3}(?:[.,]\d{1,2})?", ligne):
                    val = parse_montant_fr(mm) if "," in mm else float(mm.replace(",", "."))
                    if 1 <= val <= 400:
                        return val
    return defaut

def extraire_heures_supp(texte):
    """
    Somme des HEURES SUPPLÉMENTAIRES. Sur le format BESTT/Talentis, ce sont les lignes
    de MAJORATION (« MAJ HEURES SUP 25 % », « Majoration Heures supp de nuit … ») : la
    quantité (nombre d'heures) est le 1er montant de la ligne, PAS suivi de 'h'.
    On exclut les lignes RÉDUCTION/DÉDUCTION/COTISATION (montants, pas des heures).
    Sert à la proratisation de l'allègement (SMIC_ref, heures supp ×1,00) et à la TEPA.
    """
    total = 0.0
    for l in texte.split("\n"):
        if not re.search(r"HEURES?\s+SUP", l, re.IGNORECASE):
            continue
        if re.search(r"R[ÉE]DUCTION|D[ÉE]DUCTION|COTISATION", l, re.IGNORECASE):
            continue
        # 1) format « … 26,00 3,0775 HS … » : quantité = 1er montant, si plausible en heures
        nums = re.findall(_MONTANT_FR, l)
        if nums:
            q = parse_montant_fr(nums[0])
            if 0 < q <= 400:
                total += q
                continue
        # 2) repli : ancien format « qté h »
        m = re.search(r"([\d\u00a0 ]+,\d+)\s*h", l)
        if m:
            total += parse_montant_fr(m.group(1))
    return round(total, 2)

def _norm_libelle(s):
    """Normalise un libellé de rubrique pour rapprocher facture et BS."""
    s = re.sub(r"^\s*\d+\s*", "", s)              # retire le marqueur de ligne (1, 7, 2…)
    s = re.sub(r"\s+", " ", s).strip().upper().rstrip(" .")
    return s

def _libelle_plausible(lib):
    """Rejette les libellés parasites (texte de pied de page éclaté en lettres isolées)."""
    toks = lib.split()
    if not toks:
        return False
    isoles = sum(1 for t in toks if len(t) == 1)
    return isoles / len(toks) <= 0.5 and bool(re.search(r"[A-ZÀ-Ÿ]{3,}", lib))

def extraire_taux_bs(texte):
    """
    Extrait les taux unitaires PAYÉS de TOUTES les rubriques du bulletin
    (bloc soumis + bloc non soumis), pour la réconciliation ligne à ligne BS vs Facture.
    taux unitaire = 2e nombre de la ligne (structure : Qté, TAUX, Montant, …).
    Retourne (dict {libellé_normalisé -> taux}, set des libellés à taux VARIABLE, cout_panier).
    Les rubriques à taux variable (prime exceptionnelle, jours fériés à tarifs multiples)
    sont marquées et exclues du contrôle par coefficient.
    """
    lignes = texte.split("\n")

    def zone(debut, fin):
        cap, out = False, []
        for l in lignes:
            if re.search(debut, l, re.IGNORECASE):
                cap = True
                continue
            if cap and re.search(fin, l, re.IGNORECASE):
                cap = False
            if cap:
                out.append(l)
        return out

    z = (zone(r"RUBRIQUES SOUMISES", r"TOTAUX\s*:|CHARGES SOCIALES")
         + zone(r"RUBRIQUES NON SOUMISES",
                r"TOTAL DES RUBRIQUES NON SOUMISES|PR[ÉE]L[ÈE]VEMENT|CUMULS"))

    rates, variable = {}, set()
    for l in z:
        m = re.search(r"^(.*?)\s+-?\d[\d\u00a0 ]*,\d", l)
        if not m:
            continue
        lib = _norm_libelle(m.group(1))
        if not lib or not _libelle_plausible(lib):
            continue
        nums = re.findall(_MONTANT_FR, l)
        if len(nums) >= 2:
            taux = parse_montant_fr(nums[1])
            if lib in rates and abs(rates[lib] - taux) > 0.01:
                variable.add(lib)            # même rubrique, taux différent -> variable
            rates.setdefault(lib, taux)

    cout_panier = None
    for lib, taux in rates.items():
        if re.search(r"PANIER|TICKET|RESTAUR", lib):
            cout_panier = taux
            break
    return rates, variable, cout_panier

# Extraction des charges patronales — format BESTT/Talentis (WinDev).
# Part patronale NETTE lue sur la ligne « TOTAUX PS <montant> PP <montant> », puis on
# rajoute les réductions que l'outil re-simule (allègement RGDU, souvent sur 2 lignes :
# RETRAITE + URSSAF, et déduction patronale TEPA) pour reconstituer la part patronale BRUTE.
_RE_PP_TOTAL = re.compile(r"\bPP\s+(-?\d[\d\u00a0 .]*,\d{2})")
_RE_ALLEGEMENT = re.compile(
    r"RED\.?\s*G[ÉE]N|R[ÉE]DUCTION\s+G[ÉE]N|\bRGDU\b|FILLON|ALL[ÉE]GEMENT", re.I)
_RE_TEPA_PATRONALE = re.compile(r"D[ÉE]DUCTION\s+PATRONALE", re.I)

def extraire_charges_patronales(texte, rows=None):
    """
    Renvoie (pp_brut, pp_net, allegement_rgdu, tepa_patronale, ligne_pp, lignes_alleg).
    pp_brut = PP net + allègement RGDU + déduction patronale TEPA (part patronale AVANT
    réductions, que l'outil re-simule). (None, …) si la ligne PP est introuvable.
    """
    lignes = rows if rows else texte.split("\n")

    pp_net, ligne_pp = None, None
    for l in lignes:
        mm = _RE_PP_TOTAL.search(l)
        if mm:
            pp_net, ligne_pp = parse_montant_fr(mm.group(1)), l   # dernière ligne « PP … »

    allegement, tepa_pat, lignes_alleg = 0.0, 0.0, []
    for l in lignes:
        nums = re.findall(_MONTANT_FR, l)
        if not nums:
            continue
        if _RE_TEPA_PATRONALE.search(l):                 # déduction patronale TEPA
            tepa_pat += abs(parse_montant_fr(nums[-1])); lignes_alleg.append(l)
        elif _RE_ALLEGEMENT.search(l):                   # allègement RGDU (1 ou 2 lignes)
            allegement += abs(parse_montant_fr(nums[-1])); lignes_alleg.append(l)

    if pp_net is None:
        return None, None, 0.0, 0.0, None, []
    pp_brut = pp_net + allegement + tepa_pat
    return pp_brut, pp_net, allegement, tepa_pat, ligne_pp, lignes_alleg

# Mois de la PÉRIODE du bulletin (pour vérifier la cohérence avec le mois cible des factures).
_RE_PERIODE_BS = re.compile(
    r"[Pp]aie\s+du\s+\d{1,2}/\d{1,2}/\d{2,4}\s+au\s+\d{1,2}/(\d{1,2})/\d{2,4}")
_RE_BULLETIN_DU = re.compile(r"BULLETIN\s+DE\s+PAIE\s+DU\s+\d{1,2}/(\d{1,2})/\d{2,4}", re.I)

def mois_du_bs(texte):
    """Renvoie le mois ('06') de la période de paie du bulletin, ou None."""
    for rx in (_RE_PERIODE_BS, _RE_BULLETIN_DU):
        m = rx.search(texte)
        if m:
            return m.group(1).zfill(2)
    return None

def detecter_mois_lot_bs(fichiers_bs):
    """
    Détecte le mois DOMINANT des bulletins déposés (lecture rapide de chaque page),
    pour piloter automatiquement la consolidation des factures sur le bon mois.
    Renvoie (mois_dominant | None, dict {mois: nb_pages}).
    """
    freq = {}
    for f in fichiers_bs:
        with pdfplumber.open(f) as pdf:
            for p in pdf.pages:
                mm = mois_du_bs(p.extract_text() or "")
                if mm:
                    freq[mm] = freq.get(mm, 0) + 1
    if not freq:
        return None, {}
    return max(freq, key=freq.get), freq

def _lire_bs(fichiers_bs):
    """
    Lit chaque BS en traitant CHAQUE PAGE comme un bulletin distinct
    (un PDF de paie contient souvent tous les bulletins du mois, un par page).
    Retourne une liste de documents-pages : texte (pour l'association nom) +
    mots positionnés (pour l'extraction du brut par coordonnées).
    """
    docs = []
    for f in fichiers_bs:
        with pdfplumber.open(f) as pdf:
            n = len(pdf.pages)
            for i, p in enumerate(pdf.pages):
                texte = p.extract_text() or ""
                mots = [{"text": w["text"], "x0": w["x0"], "x1": w["x1"],
                         "top": w["top"], "bottom": w["bottom"]}
                        for w in (p.extract_words() or [])]
                label = f.name if n == 1 else f"{f.name} · p.{i + 1}"
                docs.append({"name": label, "texte": texte, "mots": mots})
    return docs

def _trouver_page_bs(docs, nom_facture):
    """
    Rattache un intérimaire à SA page de bulletin.
    Match STRICT : NOM de famille + TOUS les prénoms présents sur la page.
    En cas de 0 ou plusieurs correspondances (homonymes type TEISANU Marin /
    TEISANU Gabriel), on NE DEVINE PAS -> None (sera signalé, saisie manuelle).
    """
    mots_nom = nom_facture.split()
    nom_famille = mots_nom[0]
    prenoms = mots_nom[1:]

    correspondances = []
    for d in docs:
        t = d["texte"]
        if nom_famille not in t:
            continue
        if prenoms and not all(pr in t for pr in prenoms):
            continue
        correspondances.append(d)

    # Bulletins sur PLUSIEURS pages (ex. lignes « Rappel mission ») : les pages de
    # continuation portent le nom mais AUCUN récapitulatif « TOTAUX … SB » — elles
    # peuvent toutefois exposer un faux brut (acomptes, cumuls annuels). On ne doit pas
    # les confondre avec de vrais homonymes : on ne garde que la page portant le récap'.
    if len(correspondances) > 1:
        avec_recap = [d for d in correspondances
                      if re.search(r"TOTAUX\s*:", d["texte"])]
        if len(avec_recap) == 1:
            correspondances = avec_recap

    if len(correspondances) == 1:
        return correspondances[0], "ok"
    if len(correspondances) == 0:
        return None, "introuvable"
    return None, "ambigu"

def extraire_et_associer_bs(fichiers_bs, factures_data):
    """Associe les données de paie exactes aux factures consolidées (page par page)."""
    docs = _lire_bs(fichiers_bs)

    resultats = []
    for fact in factures_data:
        doc_cible, statut_match = _trouver_page_bs(docs, fact["interimaire"])

        if doc_cible:
            sb, base, methode_brut, ligne_brut, rows_diag = extraire_brut(
                doc_cible["texte"], doc_cible["mots"])
            heures_total = extraire_heures(doc_cible["texte"], rows_diag)
            heures_supp = extraire_heures_supp(doc_cible["texte"])
            heures_normales = max(0.0, round(heures_total - heures_supp, 2))
            taux_bs, taux_variables, cout_panier = extraire_taux_bs(doc_cible["texte"])
            pp_brut, pp_net, alleg_rgdu, tepa_pat, ligne_pp, lignes_alleg = \
                extraire_charges_patronales(doc_cible["texte"], rows_diag)
            trouve = sb is not None
            ratio_sb_base = (sb / base) if (trouve and base and base > 0) else RATIO_SB_BASE
            # Taux de charges BRUT (avant réductions) = part patronale brute ÷ SB (assiette
            # réelle des cotisations sur ce format). L'allègement/TEPA sont re-simulés ensuite.
            if pp_brut is not None and sb and sb > 0:
                taux_charges_auto = round(pp_brut / sb, 4)
                charges_trouve = True
            else:
                taux_charges_auto = TAUX_CHARGES_BASE
                charges_trouve = False
            # Indice « source CDII » : pas d'IFM (ratio ≈ 1,00) → la part patronale inclut
            # déjà la contribution FSPI/formation, à ne pas re-compter en simulation.
            source_sans_ifm = trouve and base and base > 0 and abs(ratio_sb_base - 1.0) < 0.02
            mois_bs = mois_du_bs(doc_cible["texte"])
            bs_data = {
                "brut_sb": sb if trouve else 0.0,          # brut social affiché (inclut IFM/CP)
                "total_brut": base if trouve else 0.0,     # base soumise (colonne MONTANT du BS)
                "ratio_sb_base": round(ratio_sb_base, 4),  # 1,21 (IFM+CP) ou 1,10 (IFM ou CP seul)
                "brut_trouve": trouve,                     # distingue "introuvable" de "= 0,00"
                "heures_normales": heures_normales,        # normales + nuit/dimanche + JF + solidarité
                "heures_sup": heures_supp,                 # heures supp (× 1,25 RGDU, base TEPA)
                "heures_autres": 0.0, "primes_non_soumises": 0.0,
                "heures_total": heures_total,
                "fichier_bs": doc_cible["name"], "label_brut": methode_brut,
                "ligne_brut": ligne_brut,
                "lignes_diag": rows_diag,                  # pour le panneau diagnostic
                "statut_match": statut_match,
                "taux_bs": taux_bs, "taux_variables": taux_variables, "cout_panier": cout_panier,
                "taux_charges_auto": taux_charges_auto, "charges_trouve": charges_trouve,
                "pp_brut_bs": pp_brut, "pp_net_bs": pp_net, "allegement_rgdu_bs": alleg_rgdu,
                "tepa_patronale_bs": tepa_pat, "ligne_pp": ligne_pp, "lignes_alleg_bs": lignes_alleg,
                "source_sans_ifm": source_sans_ifm, "mois_bs": mois_bs,
            }
        else:
            bs_data = {
                "brut_sb": 0.0, "total_brut": 0.0, "brut_trouve": False,
                "ratio_sb_base": RATIO_SB_BASE,
                "heures_normales": 0.0, "heures_sup": 0.0, "heures_autres": 0.0,
                "primes_non_soumises": 0.0,
                "fichier_bs": None, "label_brut": None,
                "ligne_brut": None, "lignes_diag": [],
                "statut_match": statut_match,   # "introuvable" ou "ambigu"
                "taux_bs": {}, "taux_variables": set(), "cout_panier": None,
                "taux_charges_auto": TAUX_CHARGES_BASE, "charges_trouve": False,
                "pp_brut_bs": None, "pp_net_bs": None, "allegement_rgdu_bs": 0.0,
                "tepa_patronale_bs": 0.0, "ligne_pp": None, "lignes_alleg_bs": [],
                "source_sans_ifm": False, "mois_bs": None,
            }
        resultats.append({"facture": fact, "bs": bs_data})
    return resultats

# ==========================================
# 5. MOTEUR DE CALCUL MÉTIER
# ==========================================
def calculer_comparatif(donnees, params, maj_iccp=MAJORATION_ICCP,
                        fspi_pct=10.0, formation_pct=0.0, taux_charges=None):
    facture = donnees["facture"]["total_facture"]
    lignes_facture = donnees["facture"]["lignes_retenues"]
    nom = donnees["facture"]["interimaire"]

    bs = donnees["bs"]
    # Taux de charges patronales BRUT (avant allègement) : détecté sur le BS, corrigé
    # à la main, ou repli sur le défaut paramétrable. L'allègement est re-simulé ensuite.
    tx = taux_charges if taux_charges is not None else bs.get("taux_charges_auto", TAUX_CHARGES_BASE)
    brut_base = bs["total_brut"]                    # base soumise (= SB / 1,21)
    brut_sb = bs.get("brut_sb", brut_base * RATIO_SB_BASE)  # brut social affiché (inclut IFM/CP)
    coef_detecte = facture / brut_base if brut_base > 0 else 0

    # Heures retenues pour le SMIC de référence de l'allègement : heures supp à ×1,00
    # (art. D. 241-7 : SMIC horaire × nombre d'heures supp, SANS majoration).
    # NB : la majoration réellement PAYÉE (125 %, ou 110-120 % selon le client) est déjà
    # incluse dans le brut lu sur le bulletin ; elle n'intervient donc pas ici.
    heures_ref_rgdu = bs["heures_normales"] + bs["heures_autres"] + bs["heures_sup"]
    montant_tepa = bs["heures_sup"] * params["tepa"]

    # SMIC de référence proratisé (heures supp ×1,00 ; pas de majoration caisse CP ici,
    # celle-ci est portée par le COEFFICIENT, cf. calcul_allegement).
    smic_ref_mois = params["smic_ref_horaire"] * heures_ref_rgdu

    def alleg(brut_ref, majoration):
        return calcul_allegement(params, smic_ref_mois, brut_ref, majoration)[1]

    # -- CTT PROVISIONNÉ -- (majoration caisse CP appliquée au coefficient)
    rgdu_prov = alleg(brut_base, maj_iccp)
    charges_nettes_prov = (brut_base * tx) - rgdu_prov - montant_tepa
    ifm_prov = brut_base * 0.10
    cp_prov = (brut_base + ifm_prov) * 0.10
    sequestre_total_prov = (ifm_prov + cp_prov) * (1 + tx)
    cout_total_prov = brut_base + bs["primes_non_soumises"] + charges_nettes_prov + sequestre_total_prov
    marge_prov = facture - cout_total_prov

    # -- CTT MENSUALISÉ -- (IFM/CP intégrés au brut soumis ; majoration caisse CP)
    brut_mens = brut_base + ifm_prov + cp_prov
    rgdu_mens = alleg(brut_mens, maj_iccp)
    charges_nettes_mens = (brut_mens * tx) - rgdu_mens - montant_tepa
    cout_total_mens = brut_mens + bs["primes_non_soumises"] + charges_nettes_mens
    marge_mens = facture - cout_total_mens

    # -- CDII -- (pas d'IFM ; CP PAYÉE et intégrée à l'assiette car non prise en congés ;
    #    surcotisation patronale +3.5% AKTO/FSPI ; PAS de majoration caisse CP)
    cp_cdii = brut_base * 0.10
    brut_cdii = brut_base + cp_cdii                 # CP versée mensuellement -> soumise à cotisations
    rgdu_cdii = alleg(brut_cdii, 1.0)               # aucune majoration ICCP pour le CDII
    charges_nettes_cdii = (brut_cdii * (tx + TAUX_SURCOTISATION_CDII)) - rgdu_cdii - montant_tepa
    # Contribution FSPI/AKTO : équivalent IFM (10 % du brut) affecté au fonds formation
    # (accord de branche du 10/07/2013, art. 5). Réductible par la part réellement
    # consommée en formation des intérimaires (interne/externe) ; sinon perdue.
    fspi_cdii = brut_base * (fspi_pct / 100.0) * (1 - formation_pct / 100.0)
    cout_total_cdii = brut_cdii + bs["primes_non_soumises"] + charges_nettes_cdii + fspi_cdii
    marge_cdii = facture - cout_total_cdii

    # --- CONTRÔLE DE COUVERTURE : heures FACTURÉES vs heures du BULLETIN ---
    # Les factures sont hebdomadaires : si le lot déposé ne couvre pas tout le mois du BS
    # (semaines de fin de mois facturées plus tard, facture manquante), la facturation est
    # amputée et les marges plongent artificiellement. On mesure le taux de couverture.
    # ⚠️ On ne compte QUE les heures réellement travaillées : les lignes de MAJORATION
    # (« MAJ HEURES SUP 25% » facturée au seul différentiel) ne sont pas des heures, et les
    # HEURES DE PAUSE ne figurent pas dans le total d'heures du bulletin.
    heures_facturees = round(sum(
        (l.get("qte") or 0.0) for l in lignes_facture
        if l.get("retenue") and not re.search(r"\bMAJ\b|MAJORATION|PAUSE",
                                              str(l.get("ligne", "")), re.I)), 2)
    heures_bs = bs.get("heures_total") or 0.0
    couverture = round(heures_facturees / heures_bs, 4) if heures_bs > 0 else None

    return {
        "Interimaire": nom,
        "Heures": round(bs.get("heures_total", heures_ref_rgdu), 2),
        "HeuresRefRGDU": round(heures_ref_rgdu, 2),
        "HeuresSupp": round(bs.get("heures_sup", 0.0), 2),
        "Coef": round(coef_detecte, 2),
        "BrutLu": brut_base,
        "BrutSB": brut_sb,
        "BrutTrouve": bs.get("brut_trouve", brut_base > 0),
        "TauxCharges": round(tx, 4),
        "HeuresFacturees": heures_facturees,
        "Couverture": couverture,
        "ChargesTrouve": bs.get("charges_trouve", False),
        "FichierBS": bs.get("fichier_bs"),
        "LabelBrut": bs.get("label_brut"),
        "LigneBrut": bs.get("ligne_brut"),
        "LignesDiag": bs.get("lignes_diag", []),
        "LignesRetenues": lignes_facture,
        "Marges": {
            "CTT (Provision)": round(marge_prov, 2),
            "CTT (Mensualisé)": round(marge_mens, 2),
            "CDII": round(marge_cdii, 2),
        },
        "Regime": params["regime"],
        "Data": {
            "Lignes": ["1. Facturation HT", "2. Brut Soumis", f"3. Allègement {params['regime']}",
                       "4. Séquestre ETT (IFM/CP)", "4b. FSPI/AKTO formation (CDII)",
                       "5. COÛT TOTAL", "6. MARGE NETTE"],
            "CTT (Provision)": [facture, brut_base, -rgdu_prov, sequestre_total_prov, 0.00,
                                cout_total_prov, marge_prov],
            "CTT (Mensualisé)": [facture, brut_mens, -rgdu_mens, 0.00, 0.00,
                                 cout_total_mens, marge_mens],
            "CDII": [facture, brut_cdii, -rgdu_cdii, 0.00, fspi_cdii,
                     cout_total_cdii, marge_cdii],
        },
    }

def analyser_choix_contrat(r, fspi_pct=10.0, formation_pct=0.0, cout_intermission=0.0):
    """
    Décide le contrat le plus RENTABLE et EXPLIQUE pourquoi.

    Principe : le CTT Provisionné n'est PAS un choix de marge. Après la régularisation
    progressive (obligatoire, mensuelle sur le cumul), il donne la MÊME marge que le
    Mensualisé sur la mission — seule la trésorerie diffère. On retient donc le Mensualisé
    comme marge CTT réelle, et le vrai arbitrage est CDII vs CTT, en tenant compte de la
    formation réellement documentée (récupération FSPI) et d'un éventuel coût d'intermission.
    Renvoie un dict complet pour l'affichage.
    """
    base = r["BrutLu"]
    ctt_reel = r["Marges"]["CTT (Mensualisé)"]
    cdii_net = r["Marges"]["CDII"] - cout_intermission
    delta = round(cdii_net - ctt_reel, 2)
    recommande = "CDII" if delta > 0 else "CTT"

    economie_ifm = base * 0.10                                   # IFM non versée en CDII
    fspi_plein = base * (fspi_pct / 100.0)                       # contribution FSPI totale
    fspi_perdue = fspi_plein * (1 - formation_pct / 100.0)       # part encore perdue (récupérable)
    delta_tresorerie = round(r["Marges"]["CTT (Provision)"] - ctt_reel, 2)  # gain mensuel de float

    raisons = []
    if recommande == "CDII":
        raisons.append(f"pas d'IFM : économie de {economie_ifm:.0f} € de brut (+ ses charges) "
                       "que le CTT verse en fin de mission")
        if formation_pct >= 100:
            raisons.append(f"FSPI ({fspi_plein:.0f} €) intégralement récupérée en formation")
        elif fspi_perdue > 0:
            raisons.append(f"reste {fspi_perdue:.0f} €/mois de FSPI encore récupérables "
                           "si tu documentes davantage de formation interne")
    else:
        if cout_intermission > 0:
            raisons.append(f"le coût d'intermission ({cout_intermission:.0f} €/mois) dépasse "
                           "l'économie d'IFM du CDII")
        if fspi_perdue > 0:
            raisons.append(f"la FSPI non documentée ({fspi_perdue:.0f} €/mois) est perdue — "
                           "la documenter ferait probablement basculer en faveur du CDII")

    levier = fspi_perdue if fspi_perdue > 0.5 else 0.0
    return {
        "recommande": recommande, "delta": delta,
        "cdii_net": round(cdii_net, 2), "ctt_reel": round(ctt_reel, 2),
        "economie_ifm": round(economie_ifm, 2), "fspi_plein": round(fspi_plein, 2),
        "fspi_perdue": round(fspi_perdue, 2), "levier_formation": round(levier, 2),
        "delta_tresorerie": delta_tresorerie, "raisons": raisons,
    }

def controle_couverture(dossier):
    """
    Vérifie que la FACTURATION retenue couvre bien TOUT le mois du bulletin.

    Cas typique détecté : les factures sont hebdomadaires et le lot déposé s'arrête
    avant la fin du mois (dernières semaines pas encore facturées / PDF incomplet).
    Le brut du BS porte alors sur le mois entier alors que la facture n'en couvre
    qu'une partie -> marges artificiellement basses, voire négatives.

    Compare les HEURES facturées (lignes d'heures retenues, hors lignes de simple
    MAJORATION qui ne rajoutent pas d'heures) aux heures du bulletin.
    Renvoie un dict de diagnostic.
    """
    f = dossier["facture"]
    heures_bs = dossier["bs"].get("heures_total") or 0.0
    heures_fact = 0.0
    semaines = set()
    for l in f.get("lignes_retenues", []):
        if not l.get("retenue"):
            continue
        s = re.search(r"Sem\.?\s*(\d+)", l.get("ligne", ""))
        if s:
            semaines.add(int(s.group(1)))
        lib = (l.get("libelle") or "").upper()
        # Lignes d'heures réelles : on exclut les majorations (« MAJ … », « Majoration … »),
        # dont la quantité fait doublon avec les heures déjà comptées, et les primes.
        if "HEURE" in lib and not lib.startswith("MAJ") and "MAJORATION" not in lib:
            if l.get("qte"):
                heures_fact += l["qte"]
    couverture = (heures_fact / heures_bs) if heures_bs > 0 else None
    # Trois niveaux : ≥95 % complet ; 85-95 % résidu normal de bord de mois (la dernière
    # semaine à cheval est facturée le mois suivant) ; <85 % semaines entières manquantes.
    if couverture is None:
        niveau = "inconnu"
    elif couverture >= 0.95:
        niveau = "complet"
    elif couverture >= 0.85:
        niveau = "bord_de_mois"
    else:
        niveau = "incomplet"
    return {
        "heures_facturees": round(heures_fact, 2),
        "heures_bs": round(heures_bs, 2),
        "heures_manquantes": round(max(0.0, heures_bs - heures_fact), 2),
        "couverture": couverture,
        "semaines": sorted(semaines),
        "niveau": niveau,
        "complet": niveau in ("complet", "inconnu"),
    }

def controle_coefficients(dossier, tolerance=0.02):
    """
    Réconciliation ligne à ligne BS vs Facture sur TOUTES les rubriques :
    - charges SOUMISES (heures, primes à taux fixe) attendues au coefficient commercial ;
    - charges NON SOUMISES (panier, ticket resto, transport) attendues au coefficient 1,00.
    Les rubriques à taux variable (prime exceptionnelle, jours fériés multi-tarifs) sont
    marquées « non vérifiables » et exclues du contrôle par coefficient.
    Renvoie le coef commercial détecté, la table de réconciliation et les alertes explicitées.
    """
    lignes = [l for l in dossier["facture"]["lignes_retenues"] if l.get("retenue")]
    bs = dossier["bs"]
    taux_bs = bs.get("taux_bs") or {}
    variables = bs.get("taux_variables") or set()

    def taux_paye_bs(key):
        """Taux payé BS pour une rubrique, avec tolérance OCR par mot-clé
        (ex. facture « INDEMNITE PANIER » vs BS « I2NDEMNITE PANIER »)."""
        if key in taux_bs:
            return taux_bs[key], (key in variables)
        for motcle in ("PANIER", "TICKET", "RESTAUR", "TRANSPORT"):
            if motcle in key:
                for bk, bv in taux_bs.items():
                    if motcle in bk:
                        return bv, (bk in variables)
        return None, False

    # 1) Agrège par rubrique : taux facturé DOMINANT (mode) + qté cumulée + nature
    agg = {}
    for l in lignes:
        key = _norm_libelle(l.get("libelle", ""))
        if not key:
            continue
        e = agg.setdefault(key, {"freq": {}, "soumise": l.get("soumise", True),
                                 "qte": 0.0, "libelle": l.get("libelle", "")})
        t = round(l["taux_fact"], 2) if (l.get("taux_fact") and l["taux_fact"] > 0) else None
        if t is not None:
            e["freq"][t] = e["freq"].get(t, 0) + 1
        e["qte"] += (l.get("qte") or 0)
    for e in agg.values():
        e["taux_fact"] = max(e["freq"], key=e["freq"].get) if e["freq"] else None

    # 2) Coefficient commercial = mode des coefs des lignes HEURES soumises (taux BS stable)
    coefs_h = []
    for key, e in agg.items():
        if e["soumise"] and "HEURE" in key and key not in variables:
            tp, var = taux_paye_bs(key)
            if e["taux_fact"] and tp and not var:
                coefs_h.append(round(e["taux_fact"] / tp, 2))
    coef_commercial = None
    if coefs_h:
        f = {}
        for c in coefs_h:
            f[c] = f.get(c, 0) + 1
        coef_commercial = max(f, key=f.get)

    # 3) Réconciliation ligne par ligne
    reconciliation, alertes, ecart_ns_total = [], [], 0.0
    tf_com = tp_com = base_type = None
    for key in sorted(agg):
        e = agg[key]
        tp, est_variable = taux_paye_bs(key)
        tf, soumise = e["taux_fact"], e["soumise"]
        attendu = 1.00 if not soumise else coef_commercial
        coef = round(tf / tp, 3) if (tf and tp) else None
        ecart_eur = 0.0

        if key == "HEURES NORMALES" and tf and tp:
            tf_com, tp_com, base_type = tf, tp, key

        if est_variable:
            statut = "ℹ️ non vérifiable (taux variable sur le BS)"
        elif tp is None:
            statut = "ℹ️ rubrique facturée absente du BS (à vérifier)"
        elif coef is None:
            statut = "ℹ️ taux facturé indéterminé"
        elif attendu is None:
            statut = "ℹ️ coef commercial non déterminé"
        elif abs(coef - attendu) <= tolerance:
            statut = "✅ conforme"
        else:
            ecart_eur = round((tf - attendu * tp) * (e["qte"] or 0), 2)
            statut = f"⚠️ coef {coef:.2f} ≠ {attendu:.2f} → {ecart_eur:+.2f} €"
            if not soumise:
                ecart_ns_total += ecart_eur
            alertes.append((e["libelle"], coef, attendu, soumise, ecart_eur))

        reconciliation.append({
            "libelle": e["libelle"], "soumise": soumise, "taux_fact": tf, "taux_paye": tp,
            "coef": coef, "attendu": attendu, "qte": round(e["qte"], 2),
            "variable": key in variables, "statut": statut, "ecart_eur": ecart_eur,
        })

    # 4) Messages d'alerte explicités
    messages = []
    for lib, coef, attendu, soumise, ecart in alertes:
        if not soumise:
            sens = "surfacturé au client" if ecart > 0 else "sous-facturé (perte agence)"
            messages.append(
                f"« {lib} » (non soumise) facturée au coef {coef:.2f} au lieu de 1,00 → "
                f"{ecart:+.2f} € ({sens}). Un remboursement de frais (panier, ticket resto, "
                "transport) doit être refacturé à l'euro près, sans marge.")
        else:
            messages.append(
                f"« {lib} » (soumise) facturée au coef {coef:.2f} alors que le coefficient "
                f"commercial détecté est {attendu:.2f} → {ecart:+.2f} €. Toutes les heures et "
                "primes soumises devraient suivre le même coefficient commercial.")

    return {
        "coef_commercial": coef_commercial,
        "base_type": base_type, "taux_fact_com": tf_com, "taux_paye_com": tp_com,
        "reconciliation": reconciliation,
        "ecart_ns_total": round(ecart_ns_total, 2),
        "alertes": messages,
    }

# ==========================================
# 6. INTERFACE UTILISATEUR
# ==========================================
st.set_page_config(page_title="NoPainJustGain", layout="wide")
st.title("🚀 NoPainJustGain : Audit de Marge Consolidé")

st.sidebar.header("Paramétrage Légal")

annee_cible = int(st.sidebar.number_input(
    "Année de la prestation", value=2026, step=1, format="%d",
    help="Détermine le régime d'allègement : réduction Fillon avant 2026, "
         "RGDU à compter du 1er janvier 2026."))

mois_cible = st.sidebar.selectbox(
    "Mois cible (mois du BS)",
    options=list(MOIS_LABELS.keys()),
    format_func=lambda k: f"{k} — {MOIS_LABELS[k]}",
    index=4,  # Mai par défaut
)

_EFFECTIF_MAP = {"< 20 salariés": 10, "20 à 49 salariés": 30, "≥ 50 salariés": 60}
effectif_label = st.sidebar.radio(
    "Effectif réel de l'entreprise", list(_EFFECTIF_MAP.keys()), index=0,
    help="Effectif « sécurité sociale » réel (art. L.130-1 CSS). Deux seuils distincts : "
         "FNAL/RGDU à 50 salariés (Tdelta/Tmax), TEPA à 20 salariés (1,50 € vs 0,50 €/h). "
         "Utilisé une fois le gel Loi Pacte expiré.")
effectif_reel = _EFFECTIF_MAP[effectif_label]

# --- Gel de seuil Loi Pacte (franchissement lissé sur 5 ans, art. L.130-1 II CSS) ---
# Un franchissement à la hausse n'a d'effet qu'après 5 années civiles consécutives :
# les nouvelles cotisations ne s'appliquent qu'à la 6e année. Tant que le gel court,
# on conserve le traitement d'effectif « gelé » (favorable), quel que soit l'effectif réel.
gel_pacte = st.sidebar.checkbox(
    "Gel de seuil Loi Pacte", value=True,
    help="Maintient le traitement d'effectif favorable tant que le franchissement de seuil "
         "n'est pas acquis (5 années civiles consécutives). Un franchissement à la baisse "
         "une seule année relance le compteur de 5 ans.")
if gel_pacte:
    _gel_label = st.sidebar.radio(
        "Traitement gelé (effectif de référence)", list(_EFFECTIF_MAP.keys()), index=0,
        help="Effectif retenu pour les cotisations pendant le gel (celui d'avant "
             "franchissement). « < 20 salariés » = FNAL 0,10 % + TEPA 1,50 €.")
    effectif_gel = _EFFECTIF_MAP[_gel_label]
    gel_jusqu_annee = int(st.sidebar.number_input(
        "Gel valable jusqu'au 31/12/", value=2028, step=1, format="%d",
        help="Dernière année civile du gel. Ex. : seuil franchi en 2024 → gelé 2024-2028, "
             "bascule au 1er janvier 2029."))
else:
    effectif_gel, gel_jusqu_annee = effectif_reel, annee_cible

# Effectif RETENU pour la détermination des taux (gel prioritaire tant qu'il court)
if gel_pacte and annee_cible <= gel_jusqu_annee:
    effectif = effectif_gel
    _gel_actif = True
else:
    effectif = effectif_reel
    _gel_actif = False
if gel_pacte and not _gel_actif:
    st.sidebar.warning(f"⚠️ Gel Loi Pacte expiré au 31/12/{gel_jusqu_annee} : "
                       f"l'effectif réel ({effectif_label}) s'applique pour {annee_cible}.")

_smic_ref_defaut = SMIC_REF_ANNEE.get(annee_cible, 12.02)
smic_reference = st.sidebar.number_input(
    "SMIC de référence allègement (€/h)", value=_smic_ref_defaut, step=0.01,
    help="Valeur AU 1er JANVIER, gelée sur l'année. 12,02 € pour 2026 : la hausse "
         "de juin 2026 à 12,31 € n'est PAS répercutée sur l'allègement. À mettre à "
         "jour chaque année (le SMIC change 1 à 2 fois/an).")

maj_iccp = st.sidebar.number_input(
    "Majoration caisse CP intérim (× coefficient)", value=MAJORATION_ICCP,
    step=0.001, format="%.4f",
    help="Majoration « caisse de congés payés » appliquée AU COEFFICIENT pour les CTT "
         "(pas au CDII). 1,1111 (=100/90) = la « majoration de 10 % » au sens paie, "
         "qui reproduit les chiffres officiels. Mettre 1,1000 pour un +10 % strict.")

taux_charges_defaut = st.sidebar.number_input(
    "Taux de charges patronales par défaut (%)", value=TAUX_CHARGES_BASE * 100,
    step=0.5, min_value=0.0, max_value=100.0,
    help="Repli quand le taux réel n'est pas détecté sur le bulletin. Taux patronal BRUT "
         "(avant allègement) ; l'allègement est re-simulé par l'outil.") / 100.0

# Coefficient T de la réduction Fillon éditable uniquement en régime pré-2026
_t_fillon_override = None
if annee_cible < 2026:
    _fnal_reduit = effectif < 50
    _t_def = T_FILLON.get((annee_cible, _fnal_reduit), 0.3191 if _fnal_reduit else 0.3231)
    _t_fillon_override = st.sidebar.number_input(
        f"Coefficient T Fillon {annee_cible} (FNAL {'0,10' if _fnal_reduit else '0,50'} %)",
        value=_t_def, step=0.0001, format="%.4f",
        help="⚠️ Vérifier la valeur officielle du T pour l'année auditée.")

# Régime + paramètres consolidés (indépendants de l'intérimaire)
params_regime = parametres_regime(annee_cible, effectif,
                                  smic_ref_override=smic_reference,
                                  t_fillon_override=_t_fillon_override)

st.sidebar.header("CDI Intérimaire (CDII)")
fspi_pct = st.sidebar.number_input(
    "Contribution FSPI/AKTO (% du brut)", value=10.0, step=0.5, min_value=0.0,
    help="Équivalent IFM affecté au fonds formation (accord de branche 10/07/2013, art. 5). "
         "Versé à AKTO en mars N+1. Pour le CDII uniquement.")
formation_pct = st.sidebar.slider(
    "Part récupérée par la formation (%)", 0, 100, 0,
    help="Fraction des 10 % FSPI réellement consommée en formation des intérimaires "
         "(interne/externe). 0 % = rien n'est formé → contribution perdue. "
         "100 % = intégralement récupérée en actions de formation.")
cout_intermission = st.sidebar.number_input(
    "Coût d'intermission CDII (€/mois/intérimaire)", value=0.0, step=50.0, min_value=0.0,
    help="Seul vrai risque du CDII : la garantie de rémunération versée ENTRE deux missions. "
         "Coût mensuel moyen estimé par intérimaire. 0 € si vos profils sont placés en continu "
         "(pas d'intermission).")

col1, col2 = st.columns(2)
with col1:
    fichiers_bs = st.file_uploader("📥 Déposer les Bulletins (PDF)", type=["pdf"], accept_multiple_files=True)
with col2:
    fichiers_factures = st.file_uploader("📥 Déposer les Factures (PDF)", type=["pdf"], accept_multiple_files=True)

if st.button("Lancer l'Audit Automatique", type="primary"):
    if not fichiers_factures or not fichiers_bs:
        st.warning("Veuillez déposer à la fois les Factures ET les Bulletins de Salaire.")
    else:
        with st.spinner("Détection du mois des bulletins, consolidation des factures et lecture des BS..."):
            # Le mois des BULLETINS fait autorité : on consolide les factures sur CE mois,
            # pour ne plus jamais comparer la facturation d'un mois au brut d'un autre.
            mois_bs_lot, _freq_mois = detecter_mois_lot_bs(fichiers_bs)
            mois_effectif = mois_bs_lot or mois_cible
            factures_consolidees = lire_factures_bestt_consolidees(fichiers_factures, mois_cible=mois_effectif)
            dossiers_complets = extraire_et_associer_bs(fichiers_bs, factures_consolidees)

        if mois_bs_lot and mois_bs_lot != mois_cible:
            st.info(f"📅 Mois détecté sur les bulletins : **{MOIS_LABELS[mois_bs_lot]}**. "
                    f"Les factures ont été consolidées sur **{MOIS_LABELS[mois_bs_lot]}** "
                    f"(le sélecteur était sur {MOIS_LABELS[mois_cible]}). "
                    "Pour auditer un autre mois, déposez les bulletins de ce mois-là.")

        if not dossiers_complets:
            st.error(f"Aucun intérimaire n'a pu être consolidé sur {MOIS_LABELS.get(mois_effectif, mois_effectif)}. "
                     "Vérifiez que les factures couvrent bien le mois des bulletins.")
            st.stop()

        # On mémorise les données BRUTES extraites (brut auto inclus) et on réinitialise
        # les corrections manuelles éventuelles d'un audit précédent.
        st.session_state["dossiers_audit"] = dossiers_complets
        st.session_state["mois_audit"] = mois_effectif
        st.session_state["annee_audit"] = annee_cible
        for k in [k for k in st.session_state.keys() if k.startswith("brut_")]:
            del st.session_state[k]

# ==========================================================
# RENDU (hors bouton) : recalcul à chaque modification du brut
# ==========================================================
if "dossiers_audit" in st.session_state:
    dossiers = st.session_state["dossiers_audit"]
    mois_audit = st.session_state.get("mois_audit", mois_cible)
    annee_audit = st.session_state.get("annee_audit", annee_cible)

    st.success(f"Audit — {MOIS_LABELS[mois_audit]} {annee_audit} · "
               f"régime **{params_regime['regime']}** "
               f"(FNAL {'0,10' if params_regime['fnal_reduit'] else '0,50'} %, "
               f"TEPA {params_regime['tepa']:.2f} €/h, SMIC réf. {params_regime['smic_ref_horaire']:.2f} €/h)"
               + (f" · 🔒 gel Loi Pacte actif (jusqu'au 31/12/{gel_jusqu_annee})" if _gel_actif else "")
               + ". Rapprochement Factures / BS effectué.")

    # --- GARDE-FOU : cohérence entre le mois cible (factures) et le mois des bulletins ---
    _mois_bs = [d["bs"].get("mois_bs") for d in dossiers if d["bs"].get("mois_bs")]
    if _mois_bs:
        _freq = {}
        for mm in _mois_bs:
            _freq[mm] = _freq.get(mm, 0) + 1
        _mois_bs_dominant = max(_freq, key=_freq.get)
        if _mois_bs_dominant != mois_audit:
            st.error(
                f"⛔ **Décalage de période détecté.** Les bulletins portent sur le mois "
                f"**{_mois_bs_dominant} ({MOIS_LABELS[_mois_bs_dominant]})**, mais le mois cible des "
                f"factures est **{mois_audit} ({MOIS_LABELS[mois_audit]})**. L'outil compare alors la "
                f"facturation d'un mois au brut d'un autre → marges fausses (souvent négatives). "
                f"Corrige le **« Mois cible »** dans la barre latérale sur "
                f"**{_mois_bs_dominant} — {MOIS_LABELS[_mois_bs_dominant]}** et relance l'audit.")
        else:
            st.caption(f"✅ Cohérence période : factures et bulletins sur {MOIS_LABELS[mois_audit]}.")

    # ----------------------------------------------------------
    # VÉRIFICATION / CORRECTION MANUELLE DU BRUT (garde-fou)
    # ----------------------------------------------------------
    st.header("🧾 Vérification du brut social (SB)")
    st.caption("Le brut social **SB** (colonne « = BRUT » du bloc soumis à cotisations, IFM + CP inclus) "
               "est lu automatiquement, puis la **base soumise** est dérivée (SB ÷ 1,21) pour les calculs. "
               "Le SB est modifiable : tout se recalcule aussitôt. "
               "Un SB détecté à 0,00 € n'est pas une erreur de lecture mais une anomalie de paie signalée.")

    sb_effectif = {}
    for d in dossiers:
        nom = d["facture"]["interimaire"]
        bs = d["bs"]
        auto_sb = float(bs.get("brut_sb", 0.0))
        trouve = bs.get("brut_trouve", auto_sb > 0)
        methode = bs.get("label_brut")
        fichier = bs.get("fichier_bs") or "—"
        ligne_brut = bs.get("ligne_brut")

        key = f"brut_{nom}"
        if key not in st.session_state:
            st.session_state[key] = auto_sb  # valeur initiale = SB détecté

        c_nom, c_src, c_val = st.columns([2, 3, 2])
        c_nom.markdown(f"**{nom}**")
        statut_match = bs.get("statut_match")
        if statut_match == "ambigu":
            c_src.caption("🟠 **Homonymes** : plusieurs bulletins correspondent au nom "
                          "→ rattachement impossible sans risque. Saisir le SB à droite.")
        elif not trouve:
            if statut_match == "introuvable":
                c_src.caption("🔴 **Aucun bulletin** pour cet intérimaire (facturé sans BS ?) "
                              "— saisir le SB à droite.")
            else:
                c_src.caption(f"BS : {fichier} · 🔴 **SB introuvable** — à saisir à droite")
        elif auto_sb == 0.0:
            c_src.caption(f"BS : {fichier} · ⚠️ SB détecté = 0,00 € (anomalie de paie)")
        else:
            c_src.caption(f"BS : {fichier} · ✅ {methode} → SB {auto_sb:.2f} € "
                          f"(base ≈ {auto_sb / RATIO_SB_BASE:.2f} €)")
            if ligne_brut:
                c_src.caption(f"↳ ligne : `{ligne_brut[:90]}`")
        sb_val = c_val.number_input("Brut social SB (€)", min_value=0.0, step=10.0,
                                    key=key, label_visibility="collapsed")
        sb_effectif[nom] = sb_val

    # Panneau diagnostic global : montre où le parser lit sur chaque BS
    with st.expander("🔬 Diagnostic de lecture des BS (pour identifier la bonne ligne de brut)",
                     expanded=False):
        st.caption("Pour chaque bulletin, voici les lignes reconstruites et les montants repérés. "
                   "Repérez la valeur exacte du brut : si le parser ne tombe pas dessus, "
                   "corrigez à la main ci-dessus et indiquez-moi le libellé exact de cette ligne "
                   "pour que je fiabilise la détection automatique.")
        for d in dossiers:
            nom = d["facture"]["interimaire"]
            bs = d["bs"]
            with st.expander(f"BS de {nom} — {bs.get('fichier_bs') or '—'}", expanded=False):
                lignes_diag = bs.get("lignes_diag") or []
                if not lignes_diag:
                    st.caption("Aucune ligne exploitable (PDF scanné/image ? → OCR requis).")
                choisie = bs.get("ligne_brut")
                for ligne in lignes_diag:
                    montants = re.findall(_MONTANT_FR, ligne)
                    if not montants:
                        continue
                    marqueur = "➡️ **[LIGNE RETENUE]** " if ligne == choisie else ""
                    st.markdown(f"{marqueur}`{ligne}`  —  montants : {', '.join(montants)}")

    # ----------------------------------------------------------
    # VÉRIFICATION / CORRECTION DU TAUX DE CHARGES PATRONALES
    # ----------------------------------------------------------
    st.header("🏛️ Taux de charges patronales (par dossier)")

    # Socle CTT = médiane des taux détectés sur les bulletins CTT (avec IFM/CP) du lot.
    # Sert de repli pour les bulletins CDII (taux « pollué » par le FSPI/formation que
    # l'outil re-simule) et pour les dossiers non détectés. Robuste aux valeurs aberrantes.
    _taux_ctt = [float(d["bs"]["taux_charges_auto"]) for d in dossiers
                 if d["bs"].get("charges_trouve") and not d["bs"].get("source_sans_ifm")]
    if _taux_ctt:
        _s = sorted(_taux_ctt); _n = len(_s)
        socle_ctt = round(_s[_n // 2] if _n % 2 else (_s[_n // 2 - 1] + _s[_n // 2]) / 2, 4)
    else:
        socle_ctt = taux_charges_defaut

    st.caption(f"Taux patronal **brut, AVANT réductions** = (PP net + allègement RGDU + TEPA) ÷ SB, "
               f"lu par dossier. Pour un CTT : son taux propre. Pour un CDII (sans IFM, taux gonflé "
               f"par le FSPI/formation que l'outil re-simule) ou un dossier non détecté : repli sur le "
               f"**socle CTT du lot = {socle_ctt * 100:.2f} %** (médiane des CTT). "
               "Tout reste corrigeable ci-dessous.")

    taux_charges_effectif = {}
    for d in dossiers:
        nom = d["facture"]["interimaire"]
        bs = d["bs"]
        trouve_ch = bs.get("charges_trouve", False)
        cdii_src = bs.get("source_sans_ifm", False)
        # CTT détecté → taux propre ; CDII ou non détecté → socle CTT du lot.
        if trouve_ch and not cdii_src:
            auto_tx = float(bs.get("taux_charges_auto"))
        else:
            auto_tx = socle_ctt

        key = f"txch_{nom}"
        if key not in st.session_state:
            st.session_state[key] = round(auto_tx * 100, 2)   # en %

        c_nom, c_src, c_val = st.columns([2, 3, 2])
        c_nom.markdown(f"**{nom}**")
        if trouve_ch:
            ppn = bs.get("pp_net_bs") or 0.0
            rg = bs.get("allegement_rgdu_bs") or 0.0
            tp = bs.get("tepa_patronale_bs") or 0.0
            own_tx = float(bs.get("taux_charges_auto"))
            c_src.caption(f"✅ détecté : PP net {ppn:.2f} € + RGDU {rg:.2f} € + TEPA {tp:.2f} € "
                          f"÷ SB → **{own_tx * 100:.2f} %** (brut, avant réductions)")
            if cdii_src:
                c_src.caption(f"⚠️ Bulletin **sans IFM (CDII)** — taux propre {own_tx * 100:.2f} % "
                              "gonflé par le FSPI/formation. **Socle CTT du lot appliqué** "
                              f"({socle_ctt * 100:.2f} %) pour rester comparable ; le FSPI est "
                              "re-simulé séparément. Corrigeable à droite.")
        else:
            c_src.caption(f"🔴 non détecté sur le BS — **socle CTT du lot appliqué** "
                          f"({socle_ctt * 100:.2f} %). Vérifier/saisir à droite.")
        tx_val = c_val.number_input("Taux charges (%)", min_value=0.0, max_value=100.0,
                                    step=0.5, key=key, label_visibility="collapsed")
        taux_charges_effectif[nom] = tx_val / 100.0

    # ----------------------------------------------------------
    # CALCUL (avec brut éventuellement corrigé, sans muter l'auto)
    # ----------------------------------------------------------
    master_results = []
    for d in dossiers:
        nom = d["facture"]["interimaire"]
        sb = sb_effectif[nom]
        ratio = d["bs"].get("ratio_sb_base", RATIO_SB_BASE)
        d_calc = {"facture": d["facture"],
                  "bs": {**d["bs"],
                         "brut_sb": sb,
                         "total_brut": sb / ratio}}  # base = SB ÷ ratio réel (1,21, ou 1,10 si IFM/CP seul)
        master_results.append(calculer_comparatif(d_calc, params_regime, maj_iccp=maj_iccp,
                                                  fspi_pct=fspi_pct, formation_pct=formation_pct,
                                                  taux_charges=taux_charges_effectif[nom]))

    # Contrôle des coefficients de facturation (soumises vs non soumises), par intérimaire
    controles_coef = {d["facture"]["interimaire"]: controle_coefficients(d) for d in dossiers}

    # ----------------------------------------------------------
    # SYNTHÈSE GRAPHIQUE : choix de contrat & intégration IFM/CP
    # ----------------------------------------------------------
    st.header("📊 Synthèse : choix du contrat optimal")

    # --- CONTRÔLE DE COUVERTURE : la facturation couvre-t-elle tout le mois du BS ? ---
    couvertures = {d["facture"]["interimaire"]: controle_couverture(d) for d in dossiers}
    _h_fact = sum(c["heures_facturees"] for c in couvertures.values())
    _h_bs = sum(c["heures_bs"] for c in couvertures.values())
    couv_lot = (_h_fact / _h_bs) if _h_bs > 0 else None
    _sem_lot = sorted({s for c in couvertures.values() for s in c["semaines"]})
    _fact_lue = sum(d["facture"]["total_facture"] for d in dossiers)

    if couv_lot is not None and 0 < couv_lot < 0.85:
        _manque = _fact_lue * (1 / couv_lot - 1)
        st.error(
            (f"⛔ **Facturation incomplète : {couv_lot * 100:.0f} % du mois seulement.** "
             f"Les factures déposées couvrent **{_h_fact:,.0f} h** contre **{_h_bs:,.0f} h** aux "
             f"bulletins (semaines facturées : {_sem_lot}). Le brut porte sur le mois entier, "
             f"la facturation non → **les marges ci-dessous sont sous-estimées d'environ "
             f"{_manque:,.0f} €** et peuvent apparaître négatives à tort. "
             "👉 Ajoutez les factures des semaines de fin de mois, puis relancez l'audit.")
            .replace(",", " "))
        st.info((f"📐 À couverture complète, la facturation du mois serait d'environ "
                 f"**{_fact_lue / couv_lot:,.0f} €** au lieu des {_fact_lue:,.0f} € lus. "
                 "Une marge « estimée » est affichée par dossier (indicatif, à confirmer "
                 "avec les factures réelles).").replace(",", " "))
    elif couv_lot is not None and couv_lot < 0.95:
        st.warning((f"⚠️ Couverture {couv_lot * 100:.0f} % ({_h_fact:,.0f} h facturées / "
                    f"{_h_bs:,.0f} h au bulletin, semaines {_sem_lot}). Écart habituel de "
                    "**bord de mois** : la semaine à cheval sur la fin du mois est facturée le mois "
                    "suivant. Les marges sont donc légèrement prudentes (sous-estimées).")
                   .replace(",", " "))
    elif couv_lot is not None:
        st.caption((f"✅ Couverture facturation / bulletins : {couv_lot * 100:.0f} % "
                    f"({_h_fact:,.0f} h facturées pour {_h_bs:,.0f} h au bulletin — semaines {_sem_lot}).")
                   .replace(",", " "))

    # --- ALERTE COUVERTURE : factures incomplètes vs période des bulletins ---
    _couv = [(r["Interimaire"], r["Couverture"]) for r in master_results
             if r.get("Couverture") is not None]
    _incomplets = [(n, c) for n, c in _couv if c < 0.95]
    if _incomplets:
        _moy = sum(c for _, c in _couv) / len(_couv)
        _hf = sum(r["HeuresFacturees"] for r in master_results)
        _hb = sum(r["Heures"] for r in master_results)
        _fact_tot = sum(r["Data"]["CTT (Mensualisé)"][0] for r in master_results)
        _marge_tot = sum(r["Marges"]["CTT (Mensualisé)"] for r in master_results)
        _extrap = sum(r["Marges"]["CTT (Mensualisé)"]
                      + r["Data"]["CTT (Mensualisé)"][0] * (1 / r["Couverture"] - 1)
                      for r in master_results if r.get("Couverture"))
        st.error(
            f"⛔ **Facturation incomplète — les marges affichées sont FAUSSÉES.** "
            f"{len(_incomplets)} dossier(s) sur {len(_couv)} ont moins de 95 % de leurs heures "
            f"facturées. Total : **{_hf:,.0f} h facturées contre {_hb:,.0f} h payées** "
            f"(couverture moyenne **{_moy * 100:.0f} %**). "
            "Cause habituelle : les factures sont **hebdomadaires** et le lot déposé ne couvre pas "
            "toutes les semaines du mois du bulletin (les dernières semaines sont facturées le mois "
            "suivant). Déposez **toutes les factures** couvrant la période des bulletins, puis relancez."
            .replace(",", " "))
        cA, cB = st.columns(2)
        cA.metric("Marge affichée (facturation partielle)",
                  f"{_marge_tot:,.0f} €".replace(",", " "))
        cB.metric("Marge estimée à couverture 100 %",
                  f"{_extrap:,.0f} €".replace(",", " "),
                  help="Extrapolation INDICATIVE : facturation ramenée au prorata des heures "
                       "réellement payées. À confirmer en déposant les factures manquantes.")
        with st.expander("📋 Voir la couverture par intérimaire"):
            st.dataframe(pd.DataFrame(
                [{"Intérimaire": r["Interimaire"],
                  "Heures facturées": r["HeuresFacturees"],
                  "Heures payées (BS)": r["Heures"],
                  "Couverture": f"{r['Couverture'] * 100:.0f} %"
                  if r.get("Couverture") is not None else "—"}
                 for r in master_results]), use_container_width=True, hide_index=True)
    elif _couv:
        st.success(f"✅ Couverture facturation/bulletins complète "
                   f"({min(c for _, c in _couv) * 100:.0f} % minimum). Marges exploitables.")


    rows = []
    for r in master_results:
        for statut, marge in r["Marges"].items():
            libelle = "CTT Provision (trésorerie)" if statut == "CTT (Provision)" else statut
            rows.append({"Intérimaire": r["Interimaire"], "Statut": libelle, "Marge (€)": marge})
    df_graph = pd.DataFrame(rows)

    couleurs = {"CTT Provision (trésorerie)": "#c7c7c7", "CTT (Mensualisé)": "#ff7f0e", "CDII": "#1f77b4"}
    fig = px.bar(
        df_graph, x="Intérimaire", y="Marge (€)", color="Statut",
        barmode="group", color_discrete_map=couleurs, text_auto=".0f",
        title="Marge nette par statut — CTT réel = Mensualisé, arbitrage CDII vs CTT",
    )
    fig.update_layout(legend_title_text="Statut", yaxis_title="Marge nette (€)",
                      xaxis_title="", uniformtext_minsize=8, uniformtext_mode="hide")
    fig.add_hline(y=0, line_dash="dash", line_color="grey")
    st.plotly_chart(fig, use_container_width=True)
    st.caption("⚠️ « CTT Provision » (barre claire) n'est **pas** un supplément de marge : c'est un "
               "avantage de **trésorerie** repris à la régularisation progressive. La marge CTT réelle "
               "est le **Mensualisé**. La décision se joue entre **CDII** et **CTT (Mensualisé)**.")

    # Totaux globaux par statut
    totaux = df_graph.groupby("Statut")["Marge (€)"].sum()
    c1, c2, c3 = st.columns(3)
    _labels_totaux = [("CTT (Mensualisé)", "Total CTT réel (Mensualisé)"),
                      ("CDII", "Total CDII"),
                      ("CTT Provision (trésorerie)", "Provision (trésorerie, pour info)")]
    for col, (statut, titre) in zip((c1, c2, c3), _labels_totaux):
        col.metric(titre, f"{totaux.get(statut, 0):,.0f} €".replace(",", " "))

    st.subheader("🧭 Quel contrat choisir, et pourquoi")

    with st.expander("🎓 Pourquoi (et quand) le CDII est-il le plus rentable ?", expanded=True):
        st.markdown(
            "**Le CTT Provisionné n'est pas un choix de marge.** Séquestrer les IFM/CP baisse "
            "le brut soumis et gonfle le RGDU les premiers mois, mais la **régularisation "
            "progressive** (obligatoire, mensuelle sur le cumul) reprend ce sur-allègement au "
            "mois où le séquestre est versé. Sur la mission, la marge du Provisionné = celle du "
            "**Mensualisé** ; seule la **trésorerie** diffère. Le vrai arbitrage se joue donc "
            "entre **CDII** et **CTT**.\n\n"
            "**Le CDII gagne quand deux conditions sont réunies :**\n"
            "1. **Peu ou pas d'intermission** — le CDII oblige à payer le salarié entre les "
            "missions (garantie de rémunération). Placé en continu, ce coût est nul et le CDII "
            "économise l'IFM (10 % du brut + charges) que le CTT verse en fin de mission.\n"
            "2. **Formation documentée** — la contribution FSPI/AKTO (≈ 10 % du brut) est "
            "**récupérable** si elle est consommée en formation. Non documentée, elle est perdue. "
            "Bien documentée, elle bascule d'un coût à un investissement et fait décoller la marge CDII."
        )

    reco_rows = []
    for r in master_results:
        a = analyser_choix_contrat(r, fspi_pct, formation_pct, cout_intermission)
        reco_rows.append({
            "Intérimaire": r["Interimaire"],
            "Contrat recommandé": "🔵 CDII" if a["recommande"] == "CDII" else "🟠 CTT",
            "Marge CDII nette (€)": a["cdii_net"],
            "Marge CTT réelle (€)": a["ctt_reel"],
            "Écart CDII − CTT (€)": a["delta"],
            "Levier formation (€/mois)": a["levier_formation"],
        })
    st.dataframe(pd.DataFrame(reco_rows), use_container_width=True, hide_index=True)
    st.caption("« Marge CTT réelle » = Mensualisé (= Provisionné après régularisation). "
               "« Écart » positif → CDII plus rentable. « Levier formation » = FSPI encore "
               "perdue, récupérable en documentant la formation interne.")

    # Message de synthèse global sur la formation (le gisement de marge)
    _levier_total = sum(a_["levier_formation"] for a_ in
                        (analyser_choix_contrat(r, fspi_pct, formation_pct, cout_intermission)
                         for r in master_results))
    if _levier_total > 0.5:
        st.warning(f"⚡ **Gisement de marge identifié : {_levier_total:,.0f} €/mois** de FSPI "
                   f"actuellement perdue sur ce lot ({12 * _levier_total:,.0f} €/an), récupérable "
                   "en **référençant vos formations internes existantes**. C'est le premier levier "
                   "à activer avant tout choix de contrat.".replace(",", " "))

    # Synthèse des anomalies de coefficient de facturation
    anomalies_coef = [(nom, c) for nom, c in controles_coef.items() if c["alertes"]]
    if anomalies_coef:
        st.error(f"⚠️ Anomalies de coefficient de facturation détectées sur "
                 f"{len(anomalies_coef)} intérimaire(s) : "
                 + ", ".join(nom for nom, _ in anomalies_coef)
                 + ". Détail dans chaque dossier ci-dessous.")
    else:
        st.success("✅ Contrôle des coefficients : heures soumises au coefficient commercial, "
                   "charges non soumises (panier / ticket resto) refacturées à 1,00. Aucun écart.")

    # ----------------------------------------------------------
    # VUE DÉTAILLÉE PAR SALARIÉ
    # ----------------------------------------------------------
    st.header("🔎 Détail par dossier")

    def style_dataframe(row):
        if row.name == "6. MARGE NETTE":
            is_max, is_min = row == row.max(), row == row.min()
            return [
                "background-color: #d4edda; color: #155724; font-weight: bold" if v
                else "background-color: #f8d7da; color: #721c24" if m
                else "font-weight: bold"
                for v, m in zip(is_max, is_min)
            ]
        if row.name == "3. Allègement RGDU":
            is_worst = row == row.max()  # RGDU le moins négatif = le moins d'allègement
            return ["color: #721c24; font-weight: bold" if w else "color: #155724" for w in is_worst]
        return [""] * len(row)

    for r in master_results:
        brut0 = r["BrutLu"] == 0.0
        trouve = r.get("BrutTrouve", not brut0)
        if brut0 and not trouve:
            badge = "🔴 BRUT INTROUVABLE"
        elif brut0:
            badge = "⚠️ BRUT = 0 (anomalie)"
        elif r["Coef"] and r["Coef"] > 3.0:
            badge = "⚠️ COEF ANORMAL"
        else:
            badge = ""
        analyse = analyser_choix_contrat(r, fspi_pct, formation_pct, cout_intermission)
        best = "CDII" if analyse["recommande"] == "CDII" else "CTT (Mensualisé)"

        with st.expander(
            f"Dossier : {r['Interimaire']} | Coef : {r['Coef']} | Heures : {r['Heures']}h "
            f"| Recommandé : {best} {badge}",
            expanded=True,
        ):
            if brut0 and not trouve:
                st.error("🔴 Brut introuvable sur ce bulletin : la lecture automatique a échoué. "
                         "Saisissez le brut dans « Vérification du brut social » ci-dessus "
                         "(voir aussi le panneau 🔬 Diagnostic pour repérer la bonne ligne).")
            elif brut0:
                st.warning("⚠️ Brut détecté = 0,00 € : anomalie de paie (intérimaire facturé mais "
                           "non rémunéré sur le mois). Coefficient et marges non calculables en l'état.")
            else:
                _ratio = r['BrutSB'] / r['BrutLu'] if r['BrutLu'] > 0 else RATIO_SB_BASE
                _struct = ("IFM + CP" if abs(_ratio - 1.21) < 0.02
                           else "IFM ou CP seul" if abs(_ratio - 1.10) < 0.02
                           else "structure atypique")
                st.caption(f"Brut social (SB) : **{r['BrutSB']:.2f} €** · base soumise (colonne MONTANT) "
                           f"{r['BrutLu']:.2f} € · ratio SB/base = {_ratio:.2f} ({_struct}) · "
                           f"source : {r['FichierBS']} · {r['LabelBrut']}")
                st.caption(f"⏱️ Heures : {r['Heures']:.2f} h travaillées dont {r['HeuresSupp']:.2f} h supp "
                           f"→ SMIC de réf. proratisé sur {r['HeuresRefRGDU']:.2f} h "
                           "(normales + supp × 1,00, art. D. 241-7). La majoration payée (125 % ou "
                           "110-120 % selon le client) est déjà dans le brut lu. Les heures supp basculent sous TEPA.")
                st.caption(f"🏛️ Taux de charges patronales retenu : **{r['TauxCharges'] * 100:.2f} %** "
                           + ("(détecté sur le BS)" if r.get("ChargesTrouve")
                              else "(défaut/saisie manuelle — non détecté sur le BS)")
                           + f". Surcotisation CDII +{TAUX_SURCOTISATION_CDII * 100:.1f} % en sus.")
                st.caption(f"📊 Coefficient de rentabilité global : **{r['Coef']}** "
                           "(Facture mai ÷ base soumise). Indicateur global, dilué par les postes "
                           "refacturés hors marge (panier à 1,00, primes exceptionnelles à prix coûtant) "
                           "— à ne pas confondre avec le coefficient commercial ci-dessous.")
                if r["Coef"] > 3.0:
                    st.warning(f"⚠️ Ratio global très élevé ({r['Coef']}) — possible décalage de période "
                               "facture/BS (temps partiel, brut erroné). À vérifier.")
                # Alerte sur le VRAI indicateur : le coefficient commercial (main-d'œuvre)
                _ctrl = controles_coef.get(r["Interimaire"])
                _cc = _ctrl["coef_commercial"] if _ctrl else None
                if _cc is None:
                    st.caption("Coefficient commercial non déterminé (pas de ligne d'heures exploitable).")
                elif _cc < 1.82:
                    st.error(f"⚠️ Coefficient commercial bas ({_cc:.2f}) — seuil conseillé ≥ 1,82 : "
                             "sous-facturation probable de la main-d'œuvre.")
                else:
                    st.success(f"✅ Coefficient commercial validé : {_cc:.2f} "
                               "(main-d'œuvre facturée au bon taux).")
                # --- COUVERTURE DE LA FACTURATION SUR LE MOIS ---
                _cv = couvertures.get(r["Interimaire"])
                if _cv and _cv["couverture"] is not None and _cv["niveau"] == "incomplet":
                    _f = r["Data"]["CTT (Provision)"][0]      # facturation HT retenue
                    _redress = _f * (1 / _cv["couverture"] - 1)
                    st.error(
                        f"⛔ Facturation incomplète : **{_cv['couverture'] * 100:.0f} %** du mois "
                        f"({_cv['heures_facturees']:.0f} h facturées / {_cv['heures_bs']:.0f} h au "
                        f"bulletin, semaines {_cv['semaines']} → il manque ~{_cv['heures_manquantes']:.0f} h). "
                        f"Marges ci-dessous sous-estimées d'environ **{_redress:,.0f} €**. "
                        f"Marge CTT estimée à couverture complète : "
                        f"**{analyse['ctt_reel'] + _redress:+,.0f} €** · CDII : "
                        f"**{analyse['cdii_net'] + _redress:+,.0f} €**.".replace(",", " "))
                elif _cv and _cv["niveau"] == "bord_de_mois":
                    st.caption(f"⚠️ Couverture {_cv['couverture'] * 100:.0f} % "
                               f"({_cv['heures_facturees']:.0f} h / {_cv['heures_bs']:.0f} h, semaines "
                               f"{_cv['semaines']}) — résidu normal de bord de mois, marge un peu prudente.")
                elif _cv and _cv["couverture"] is not None:
                    st.caption(f"✅ Couverture facturation : {_cv['couverture'] * 100:.0f} % "
                               f"({_cv['heures_facturees']:.0f} h / {_cv['heures_bs']:.0f} h, "
                               f"semaines {_cv['semaines']}).")

                # --- RECOMMANDATION DE CONTRAT (le cœur de la décision) ---
                if analyse["recommande"] == "CDII":
                    msg = (f"🔵 **CDII recommandé** : marge {analyse['cdii_net']:.0f} € "
                           f"vs {analyse['ctt_reel']:.0f} € en CTT (**{analyse['delta']:+.0f} €/mois**).")
                    if analyse["raisons"]:
                        msg += " Pourquoi : " + " ; ".join(analyse["raisons"]) + "."
                    st.info(msg)
                    if analyse["levier_formation"] > 0.5:
                        st.success(f"⚡ Levier : documenter la formation interne récupérerait jusqu'à "
                                   f"**+{analyse['levier_formation']:.0f} €/mois** de FSPI aujourd'hui perdue.")
                else:
                    msg = (f"🟠 **CTT recommandé** : marge {analyse['ctt_reel']:.0f} € "
                           f"vs {analyse['cdii_net']:.0f} € en CDII ({analyse['delta']:+.0f} €/mois).")
                    if analyse["raisons"]:
                        msg += " Pourquoi : " + " ; ".join(analyse["raisons"]) + "."
                    st.warning(msg)
                st.caption(f"ℹ️ Le CTT **Provisionné** afficherait {analyse['delta_tresorerie']:+.0f} €/mois "
                           "de plus, mais c'est un gain de **trésorerie** repris à la régularisation "
                           "progressive — même marge réelle que le Mensualisé. Il n'entre pas dans l'arbitrage.")

            # --- CONTRÔLE DES COEFFICIENTS DE FACTURATION (réconciliation BS vs Facture) ---
            ctrl = controles_coef.get(r["Interimaire"])
            if ctrl and ctrl["reconciliation"]:
                cc = ctrl["coef_commercial"]
                entete = (f"**Contrôle des coefficients (BS vs Facture)** — coef commercial "
                          f"détecté : **{cc:.2f}**" if cc is not None
                          else "**Contrôle des coefficients (BS vs Facture)**")
                st.markdown(entete)

                lignes_reco = []
                for x in ctrl["reconciliation"]:
                    lignes_reco.append({
                        "Rubrique": x["libelle"],
                        "Nature": "soumise" if x["soumise"] else "non soumise",
                        "Taux facturé": f"{x['taux_fact']:.2f} €" if x["taux_fact"] else "—",
                        "Taux payé (BS)": f"{x['taux_paye']:.2f} €" if x["taux_paye"] else "—",
                        "Coef appliqué": f"{x['coef']:.2f}" if x["coef"] else "—",
                        "Coef attendu": (f"{x['attendu']:.2f}" if x["attendu"] is not None else "—"),
                        "Statut": x["statut"],
                    })
                st.dataframe(pd.DataFrame(lignes_reco), use_container_width=True, hide_index=True)

                if ctrl["alertes"]:
                    for al in ctrl["alertes"]:
                        st.error("⚠️ " + al)
                else:
                    st.success("✅ Réconciliation conforme : heures et primes soumises au coefficient "
                               "commercial, charges non soumises (panier / ticket resto / transport) "
                               "refacturées à 1,00. Les rubriques à taux variable ou absentes du BS "
                               "sont signalées pour vérification manuelle.")

            # --- AFFICHAGE OPTIONNEL DES LIGNES DE FACTURE (déployé au clic) ---
            lignes = r["LignesRetenues"]
            nb_retenues = sum(1 for l in lignes if l["retenue"])
            with st.expander(
                f"🔍 Voir le détail de la consolidation "
                f"({nb_retenues} ligne(s) retenue(s) / {len(lignes)} analysée(s)) — cliquer pour déployer",
                expanded=False,
            ):
                montrer_ignorees = st.checkbox(
                    "Afficher aussi les lignes ignorées (autres mois)",
                    value=False, key=f"chk_{r['Interimaire']}",
                )
                for l in lignes:
                    if l["retenue"] or montrer_ignorees:
                        st.caption(f"{l['statut']} · [{l['fichier']}] "
                                   f"{l['montant']:.2f} € — {l['ligne']}")

            # --- TABLEAU COMPARATIF ---
            df = pd.DataFrame(r["Data"]).set_index("Lignes")
            st.dataframe(
                df.style.format("{:.2f} €").apply(style_dataframe, axis=1),
                use_container_width=True,
            )
            st.caption(
                f"ℹ️ Allègement calculé en régime **{r.get('Regime', '—')}** "
                f"(Fillon avant 2026, RGDU à partir de 2026). "
                "**CTT** : majoration caisse de congés payés appliquée **au coefficient** "
                f"(×{maj_iccp:.4f}). **CDII** : **aucune** majoration (contrat CDI) ; "
                "surcotisation patronale +3,5 % (AKTO/FSPI) et CP payés intégrés à l'assiette. "
                "Assiette par statut — Provisionné : base ; Mensualisé : SB (IFM + CP soumis) ; "
                "CDII : base + CP. Coefficient plafonné à Tmax ; plancher 2 % puis 0 au-delà de 3 SMIC (RGDU)."
            )
