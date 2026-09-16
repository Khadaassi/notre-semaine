def _ing(name, quantity=None, unit=''):
    """Builds one Recipe.ingredients entry (Lot 4 format). Quantities aren't
    filled in here on purpose — DEFAULT_RECIPES are generic seed recipes and
    we don't want to invent amounts nobody specified; a family can add real
    quantities/units via the recipe form once they use them."""
    return {'name': name, 'quantity': quantity, 'unit': unit}


DEFAULT_RECIPES = [
    {'name': 'Poulet rôti aux herbes & légumes racines', 'category': 'Viande', 'art_key': 'chicken',
     'ingredients': [_ing('Poulet'), _ing('Pommes de terre'), _ing('Carottes'), _ing('Panais'),
                     _ing('Ail'), _ing("Huile d'olive"), _ing('Romarin')]},
    {'name': "Tajine d'agneau", 'category': 'Viande', 'art_key': 'lamb',
     'ingredients': [_ing('Agneau'), _ing('Oignon'), _ing('Épices tajine'), _ing('Amandes'), _ing('Riz')]},
    {'name': 'Sauté de bœuf aux poivrons et riz', 'category': 'Viande', 'art_key': 'beef',
     'ingredients': [_ing('Bœuf'), _ing('Poivrons'), _ing('Oignon'), _ing('Ail'),
                     _ing('Tamari (sauce soja sans gluten)'), _ing('Riz')]},
    {'name': 'Boulettes de bœuf, sauce tomate et riz', 'category': 'Viande', 'art_key': 'beef',
     'ingredients': [_ing('Bœuf haché'), _ing('Œuf'), _ing('Oignon'), _ing('Tomates concassées'), _ing('Riz')]},
    {'name': 'Poulet au curry et lait de coco', 'category': 'Viande', 'art_key': 'chicken',
     'ingredients': [_ing('Poulet'), _ing('Lait de coco'), _ing('Curry'), _ing('Oignon'),
                     _ing('Poivron'), _ing('Riz')]},
    {'name': 'Poulet mariné au citron et herbes, riz', 'category': 'Viande', 'art_key': 'chicken',
     'ingredients': [_ing('Poulet'), _ing('Citron'), _ing('Ail'), _ing('Herbes de Provence'),
                     _ing("Huile d'olive"), _ing('Riz')]},
    {'name': 'Brochettes de bœuf, poivrons et oignons', 'category': 'Viande', 'art_key': 'skewer',
     'ingredients': [_ing('Bœuf'), _ing('Poivrons'), _ing('Oignon'), _ing('Cumin'),
                     _ing('Paprika'), _ing("Huile d'olive")]},
    {'name': 'Poulet aux olives et citron confit', 'category': 'Viande', 'art_key': 'chicken',
     'ingredients': [_ing('Poulet'), _ing('Olives'), _ing('Citron confit'), _ing('Oignon'),
                     _ing('Ail'), _ing('Riz')]},
    {'name': "Sauté d'agneau aux légumes printaniers", 'category': 'Viande', 'art_key': 'lamb',
     'ingredients': [_ing('Agneau'), _ing('Carottes'), _ing('Petits pois'), _ing('Oignon'),
                     _ing('Ail'), _ing("Huile d'olive")]},
    {'name': 'Hachis de bœuf, écrasé de patate douce', 'category': 'Viande', 'art_key': 'beef',
     'ingredients': [_ing('Bœuf haché'), _ing('Patate douce'), _ing('Oignon'),
                     _ing('Tomates concassées'), _ing("Huile d'olive")]},
    {'name': 'Cabillaud vapeur, purée de patate douce', 'category': 'Poisson', 'art_key': 'fish',
     'ingredients': [_ing('Cabillaud'), _ing('Patate douce'), _ing('Citron'), _ing("Huile d'olive")]},
    {'name': 'Saumon grillé, quinoa et brocolis', 'category': 'Poisson', 'art_key': 'fish',
     'ingredients': [_ing('Saumon'), _ing('Quinoa'), _ing('Brocolis'), _ing('Citron')]},
    {'name': 'Papillote de dorade aux légumes', 'category': 'Poisson', 'art_key': 'fish',
     'ingredients': [_ing('Dorade'), _ing('Courgette'), _ing('Tomate'), _ing('Citron'), _ing('Olives')]},
    {'name': 'Curry de pois chiches et épinards', 'category': 'Végétarien', 'art_key': 'veggie',
     'ingredients': [_ing('Pois chiches'), _ing('Épinards'), _ing('Lait de coco'), _ing('Curry'), _ing('Riz')]},
    {'name': 'Chili végétarien (haricots rouges)', 'category': 'Végétarien', 'art_key': 'veggie',
     'ingredients': [_ing('Haricots rouges'), _ing('Maïs'), _ing('Tomates concassées'),
                     _ing('Poivron'), _ing('Riz')]},
    {'name': 'Buddha bowl quinoa & légumes rôtis', 'category': 'Végétarien', 'art_key': 'veggie',
     'ingredients': [_ing('Quinoa'), _ing('Patate douce'), _ing('Brocolis'), _ing('Pois chiches'), _ing('Tahini')]},
    {'name': 'Galettes de lentilles corail', 'category': 'Végétarien', 'art_key': 'veggie',
     'ingredients': [_ing('Lentilles corail'), _ing('Carotte'), _ing('Oignon'), _ing('Œuf'), _ing('Farine de riz')]},
    {'name': 'Taboulé de boulgour, avocat et citron', 'category': 'Végétarien', 'art_key': 'veggie',
     'ingredients': [_ing('Boulgour'), _ing('Avocat'), _ing('Citron'), _ing('Oignon'),
                     _ing('Persil'), _ing("Huile d'olive")]},
    {'name': 'Velouté de courge butternut', 'category': 'Soupe', 'art_key': 'soup',
     'ingredients': [_ing('Courge butternut'), _ing('Oignon'), _ing('Bouillon de légumes'), _ing('Lait de coco')]},
    {'name': 'Soupe de lentilles corail et carottes', 'category': 'Soupe', 'art_key': 'soup',
     'ingredients': [_ing('Lentilles corail'), _ing('Carottes'), _ing('Oignon'), _ing('Cumin'),
                     _ing('Bouillon de légumes')]},
    {'name': 'Bouillon de poulet, légumes et vermicelles de riz', 'category': 'Soupe', 'art_key': 'soup',
     'ingredients': [_ing('Poulet'), _ing('Carottes'), _ing('Vermicelles de riz'), _ing('Bouillon de volaille')]},
    {'name': 'Omelette aux légumes et pommes de terre', 'category': 'Autre', 'art_key': 'egg',
     'ingredients': [_ing('Œufs'), _ing('Pommes de terre'), _ing('Oignon'), _ing('Poivron'),
                     _ing("Huile d'olive")]},
]

DEFAULT_GROCERY = [
    ('Fruits & légumes', ['Fruits de saison', 'Légumes de saison', 'Salade', 'Bananes']),
    ('Féculents & épicerie', ['Pain', 'Riz / pâtes', 'Farine', "Huile d'olive"]),
    ('Protéines', ['Viande / volaille', 'Poisson', 'Œufs', 'Légumineuses']),
    ('Produits laitiers', ['Lait', 'Yaourts', 'Fromage']),
    ('Hygiène & entretien', ['Produit vaisselle', 'Lessive', 'Papier toilette', 'Savon']),
]

DEFAULT_ACTIVITIES = []
