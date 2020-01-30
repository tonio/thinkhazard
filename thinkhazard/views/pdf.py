# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2017 by the GFDRR / World Bank
#
# This file is part of ThinkHazard.
#
# ThinkHazard is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# ThinkHazard is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along with
# ThinkHazard.  If not, see <http://www.gnu.org/licenses/>.

import datetime
import logging
import re

import os
import traceback
from uuid import uuid4

from os import path
from typing import List

from subprocess import Popen, PIPE
from time import time, sleep

from pyramid.view import view_config
from pyramid.httpexceptions import HTTPBadRequest, HTTPNotFound
from pyramid.response import FileResponse

from io import BytesIO
from PyPDF2 import PdfFileReader, PdfFileWriter
from asyncio import run
from pyppeteer import launch

from .report import (
    _hazardlevel_nodata,
    get_division,
    get_hazard_types,
    get_info_for_hazard_type,
    get_parents,
)

from ..models import (
    DBSession,
    AdministrativeDivision,
    HazardLevel,
    HazardCategory,
    HazardCategoryAdministrativeDivisionAssociation,
)

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from geoalchemy2.functions import ST_Centroid

REPORT_ID_REGEX = re.compile("\d{4}_\d{2}_\w{8}(-\w{4}){3}-\w{12}?")

logger = logging.getLogger(__name__)


@view_config(route_name="pdf_cover", renderer="templates/pdf_cover.jinja2")
def pdf_cover(request):
    try:
        division_code = request.matchdict.get("divisioncode")
    except:
        raise HTTPBadRequest(detail="incorrect value for parameter " '"divisioncode"')
    division = get_division(division_code)
    hazard_types = get_hazard_types(division_code)

    hazards_sorted = sorted(hazard_types, key=lambda a: a["hazardlevel"].order)

    hazard_categories = []
    for h in hazards_sorted:
        if h["hazardlevel"].mnemonic == _hazardlevel_nodata.mnemonic:
            continue
        hazard_categories.append(
            get_info_for_hazard_type(request, h["hazardtype"].mnemonic, division)
        )

    lon, lat = (
        DBSession.query(
            func.ST_X(ST_Centroid(AdministrativeDivision.geom)),
            func.ST_Y(ST_Centroid(AdministrativeDivision.geom)),
        )
        .filter(AdministrativeDivision.code == division_code)
        .first()
    )

    context = {
        "hazards": hazard_types,
        "hazards_sorted": sorted(hazard_types, key=lambda a: a["hazardlevel"].order),
        "parents": get_parents(division),
        "division": division,
        "division_lonlat": (lon, lat),
        "hazard_categories": hazard_categories,
        "date": datetime.datetime.now(),
    }

    return context


"""pdf_about: see index.py"""


async def create_pdf(file_name: str, file_name_temp: str, pages: List[str]):
    """Create a PDF file with the given pages using pyppeteer.
    """

    # Create temporary file
    open(file_name_temp, "a").close()
    chunks = []

    # --no-sandbox is required to make Chrome/Chromium run under root.
    browser = await launch(
        handleSIGINT=False,
        handleSIGTERM=False,
        handleSIGHUP=False,
        args=["--no-sandbox"],
    )
    page = await browser.newPage()
    for url in pages:
        await page.goto(url, {"waitUntil": "networkidle0"})
        chunks.append(
            BytesIO(await page.pdf({"format": "A4", "printBackground": True}))
        )
    await browser.close()

    # merge all pages
    writer = PdfFileWriter()
    # for page in pages:
    for chunk in chunks:
        reader = PdfFileReader(chunk)
        for index in range(reader.numPages):
            writer.addPage(reader.getPage(index))
    output = open(file_name, "wb")
    writer.write(output)
    output.close()

    try:
        os.remove(file_name_temp)
    except OSError:
        pass


@view_config(route_name="create_pdf_report", request_method="POST", renderer="json")
def create_pdf_report(request):
    """View to create an asynchronous print job.
    """
    division_code = get_divison_code(request)

    base_path = request.registry.settings.get("pdf_archive_path")
    report_id = _get_report_id(division_code, base_path)

    categories = (
        DBSession.query(HazardCategory)
        .options(joinedload(HazardCategory.hazardtype))
        .join(HazardCategoryAdministrativeDivisionAssociation)
        .join(AdministrativeDivision)
        .join(HazardLevel)
        .filter(AdministrativeDivision.code == division_code)
        .order_by(HazardLevel.order)
    )

    query_args = {"_query": {"_LOCALE_": request.locale_name}}

    pages = [
        request.route_url("pdf_cover", divisioncode=division_code, **query_args),
        request.route_url("pdf_about", **query_args),
    ]
    for cat in categories:
        pages.append(
            request.route_url(
                "report_print",
                divisioncode=division_code,
                hazardtype=cat.hazardtype.mnemonic,
                **query_args,
            )
        )

    file_name = _get_report_filename(base_path, division_code, report_id)
    file_name_temp = _get_report_filename(
        base_path, division_code, report_id, temp=True
    )

    # already create the file, so that the client can poll the status
    _touch(file_name_temp)

    run(create_pdf(file_name, file_name_temp, pages))

    return {"divisioncode": division_code, "report_id": report_id}


@view_config(route_name="get_report_status", renderer="json")
def get_report_status(request):
    """View to poll the status of a print job.
    """
    division_code = get_divison_code(request)
    report_id = get_report_id(request)

    base_path = request.registry.settings.get("pdf_archive_path")
    file_name = _get_report_filename(base_path, division_code, report_id)
    file_name_temp = _get_report_filename(
        base_path, division_code, report_id, temp=True
    )

    if path.isfile(file_name):
        return {"status": "done"}
    elif path.isfile(file_name_temp):
        return {"status": "running"}
    else:
        raise HTTPNotFound("No job found or job has failed")


@view_config(route_name="get_pdf_report")
def get_pdf_report(request):
    """Return the PDF file for finished print jobs.
    """
    division_code = get_divison_code(request)
    report_id = get_report_id(request)

    base_path = request.registry.settings.get("pdf_archive_path")
    file_name = _get_report_filename(base_path, division_code, report_id)
    file_name_temp = _get_report_filename(
        base_path, division_code, report_id, temp=True
    )

    if path.isfile(file_name):
        division_name = (
            DBSession.query(AdministrativeDivision.name)
            .filter(AdministrativeDivision.code == division_code)
            .scalar()
        )
        response = FileResponse(
            file_name, request=request, content_type="application/pdf"
        )
        response.headers["Content-Disposition"] = (
            'attachment; filename="ThinkHazard - %s.pdf"'
            % division_name)
        return response
    elif path.isfile(file_name_temp):
        return HTTPBadRequest("Not finished yet")
    else:
        raise HTTPNotFound("No job found")


def _get_report_id(division_code, base_path):
    """Generate a random report id. Check that there is no existing file with
    the generated id.
    """
    while True:
        date = datetime.datetime.now()
        year = date.strftime("%Y")
        month = date.strftime("%m")
        report_id = "_".join([year, month, str(uuid4())])
        file_name = _get_report_filename(base_path, division_code, report_id)
        file_name_temp = _get_report_filename(
            base_path, division_code, report_id, temp=True
        )
        if not (path.isfile(file_name) or path.isfile(file_name_temp)):
            return report_id


def _get_report_filename(base_path, division_code, report_id, temp=False):
    year, month, id = report_id.split("_")
    return path.join(
        base_path,
        year,
        month,
        ("_" if temp else "") + "{:s}-{:s}.pdf".format(division_code, id),
    )


def _touch(file):
    path = os.path.dirname(file)
    if not os.path.exists(path):
        os.makedirs(path)
    with open(file, "a"):
        os.utime(file, None)


def get_divison_code(request):
    try:
        return request.matchdict.get("divisioncode")
    except:
        raise HTTPBadRequest(detail="incorrect value for parameter " '"divisioncode"')


def get_report_id(request):
    report_id = request.matchdict.get("id")
    if report_id and REPORT_ID_REGEX.match(report_id):
        return report_id
    else:
        raise HTTPBadRequest(detail="incorrect value for parameter " '"report_id"')
