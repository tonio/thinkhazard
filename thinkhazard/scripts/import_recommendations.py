import os
import sys
import transaction
import csv

from sqlalchemy import engine_from_config

from pyramid.paster import (
    get_appsettings,
    setup_logging,
    )

from thinkhazard_common.models import (
    DBSession,
    HazardType,
    HazardLevel,
    HazardCategory,
    TechnicalRecommendation,
    HazardCategoryTechnicalRecommendationAssociation,
    )

from .. import load_local_settings


def usage(argv):
    cmd = os.path.basename(argv[0])
    print('usage: %s <config_uri>\n'
          '(example: "%s development.ini")' % (cmd, cmd))
    sys.exit(1)


def main(argv=sys.argv):
    if len(argv) < 2:
        usage(argv)
    config_uri = argv[1]
    setup_logging(config_uri)
    settings = get_appsettings(config_uri)

    load_local_settings(settings)

    engine = engine_from_config(settings, 'sqlalchemy.')

    DBSession.configure(bind=engine)

    with transaction.manager:
        DBSession.query(HazardCategoryTechnicalRecommendationAssociation) \
            .delete()
        DBSession.query(TechnicalRecommendation).delete()
        DBSession.query(HazardCategory).delete()

        # First load general recommendations
        # the csv file is an export of
        # https://docs.google.com/spreadsheets/d/12NfZBxhRmruHD3LAJGzesoLvt9_7fC2KHlbvqAab0rM  # NOQA

        path = os.path.dirname(os.path.abspath(__file__))
        with open(path + '/general_recommendations.csv', 'rb') as csvfile:
            recommendations = csv.reader(csvfile, delimiter=',')
            for row in recommendations:

                hazard_category = HazardCategory(**{
                    'general_recommendation': row[2]
                })
                hazard_category.hazardtype = DBSession.query(HazardType) \
                    .filter(HazardType.mnemonic == row[0]).one()
                hazard_category.hazardlevel = DBSession.query(HazardLevel) \
                    .filter(HazardLevel.mnemonic == row[1]).one()
                DBSession.add(hazard_category)

        categories = []
        for type in [u'EQ', u'FL', u'CY', u'TS', u'SS', u'VA', u'DG']:
            for level in [u'HIG', u'MED', u'LOW', u'NPR']:
                hazardcategory = DBSession.query(HazardCategory) \
                    .join(HazardLevel) \
                    .join(HazardType) \
                    .filter(HazardLevel.mnemonic == level) \
                    .filter(HazardType.mnemonic == type) \
                    .one()
                categories.append(hazardcategory)

        # Then technical recommendations
        # the csv file is an export of
        # https://docs.google.com/spreadsheets/d/1lRUauLAiM-DPHohHklZnNXDm4FbNTXiFw-GCq9BztaY  # NOQA

        hctra = HazardCategoryTechnicalRecommendationAssociation

        with open(path + '/technical_recommendations.csv', 'rb') as csvfile:
            recommendations = csv.reader(csvfile, delimiter=',')
            for row in recommendations:
                technical_rec = TechnicalRecommendation(**{
                    'text': row[0]
                })
                associations = technical_rec.hazardcategory_associations

                # the other columns are hazard category (type / level)
                for col_index in range(1, 28):
                    value = row[col_index]
                    if value is not '' and value is not 'Y':
                        association = hctra(order=value)
                        association.hazardcategory = categories[col_index - 1]
                        associations.append(association)
                DBSession.add(technical_rec)
