import datetime
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor as PoolExecutor
from pathlib import Path

from core.aws.comprehend import ExtractBirthday, ExtractExhibition, ExtractName
from core.aws.s3 import create_bucket, exists_file, read_file, upload_file, upload_text
from core.aws.textract import process_file
from core.convert import data2pdf

from config import AWS_BUCKET_NAME

exhibition = ExtractExhibition()


class Parser:

    # file locations
    TMP_FILE = "tmp/{hash}.pdf"
    TEXTRACT_JSON = "tmp/{hash}.json"

    ORIGINAL_FILE = "cvs/{name}/cv.pdf"
    TEXTRACT_FILE = "cvs/{name}/textract.json"
    PARSED_JSON = "cvs/{name}/parsed.json"
    PARSED_PDF = "cvs/{name}/parsed.pdf"

    def __init__(self, **config):
        self.emit = config.get("emit", None)
        self.meta = config.get("meta", {})

    def dispatch(self, code, service, status, info=None, meta=None):
        result = {
            "code": code,
            "service": service,
            "status": status,
            "info": info,
            "meta": meta,
        }

        # emit to socket or print
        if self.emit:
            self.emit("job:message", result)

        # print to screen
        print(result)

    def process_blocks(self, blocks):

        # default result
        result = {
            "name": None,
            "dob": None,
            "solo_exhibitions": [],
            "group_exhibitions": [],
        }

        # filter blocks
        blocks = [b for b in blocks if b["BlockType"] in ["LINE"]]

        if not blocks:
            return result

        header_text = ""

        # extract header text
        for b in blocks:
            if len(header_text) >= 500:
                break

            header_text += b.get("Text", "") + ". "

        self.dispatch("welp", "script", "Header extracted.", header_text)

        # extract name
        result["name"] = ExtractName(header_text)
        self.dispatch("artist:name", "script", "Name detected.", result["name"])

        # extract dob
        result["dob"] = ExtractBirthday(header_text)
        self.dispatch("artist:dob", "script", "DOB detected.", result["dob"])

        # extract sections

        sections = [
            {
                "name": "Solo Exhibitions",
                "keywords": [
                    "individual exhibition",
                    "solo exhibition",
                    "person exhibition",
                ],
                "slug": "solo_exhibitions",
            },
            {
                "name": "Group Exhibitions",
                "keywords": ["group exhibition", "selected exhibition"],
                "slug": "group_exhibitions",
            },
        ]

        for section in sections:
            self.dispatch("welp", "script", "Searching for %s." % section["name"])

            section_found = False
            section_end_index = 0
            section_year_indexes = []

            # gather all years in section
            for i, b in enumerate(blocks):
                text = b.get("Text", "").lower()

                if not text:
                    continue

                if [s for s in section["keywords"] if s in text] and not section_found:
                    section_found = True
                    self.dispatch(
                        "welp",
                        "script",
                        "Found %s starting." % section["name"],
                        i,
                        {"index": i},
                    )

                if not section_found:
                    continue

                years = re.findall(r"^(?:19|20)\d{2}", text)

                if years:
                    year = years[0]
                    section_year_indexes.append((i, year))

                # section has ended
                if (
                    sorted(section_year_indexes, key=lambda y: y[1], reverse=True)
                    != section_year_indexes
                ):
                    section_end_index = i - 1
                    section_year_indexes = section_year_indexes[:-1]
                    self.dispatch(
                        "welp",
                        "script",
                        "Found %s ending." % section["name"],
                        i - 1,
                        {"index": i - 1},
                    )
                    break

            self.dispatch(
                "welp",
                "script",
                "Identified years.",
                len(section_year_indexes),
                section_year_indexes,
            )

            for j, (i, year) in enumerate(section_year_indexes):
                exhibition_start_index = i
                exhibition_end_index = (
                    section_year_indexes[j + 1][0]
                    if j + 1 < len(section_year_indexes)
                    else section_end_index
                )

                # iterate over all exhibitions between years
                for x in range(exhibition_start_index, exhibition_end_index):
                    text = blocks[x]
                    text = re.sub(r"^(?:19|20)\d{2}", "", blocks[x]["Text"])
                    text = text.strip()

                    if not text:
                        continue

                    exhibition = ExtractExhibition()
                    title = exhibition.process(year=year, text=text)

                    exhibition_result = {
                        "year": year,
                        "title": title,
                        "original": text,
                        "type": section["slug"],
                    }

                    if title:
                        self.dispatch(
                            "artist:exhibition",
                            "comprehend",
                            "Found exhibition: %s" % title,
                            title,
                            exhibition_result,
                        )

                    result[section["slug"]].append(exhibition_result)

        return result

    def process_cv(self, file_path):

        # cv meta
        meta = {"hash": None}

        print('before hashhhhhhhhhhhhhhh')

        # identify file uniquely by content
        file_hash = hashlib.md5(open(file_path, "rb").read()).hexdigest()
        print('after hashhhhhhhhhhhhhhh', file_hash)

        meta["hash"] = file_hash
        self.dispatch("file:hash", "hash", "File hash computed.", file_hash)

        file_temp = self.TMP_FILE.format(hash=file_hash)

        # create bucket if not exists
        if create_bucket(bucket=AWS_BUCKET_NAME):
            self.dispatch("welp", "s3", "S3 Bucket created.", AWS_BUCKET_NAME)

        # check if temp file exists in s3
        if not exists_file(bucket=AWS_BUCKET_NAME, object_name=file_temp):
            response = upload_file(
                file_path=file_path, bucket=AWS_BUCKET_NAME, object_name=file_temp
            )
            self.dispatch("welp", "s3", "PDF uploaded to s3 bucket.", response)
        else:
            self.dispatch("welp", "s3", "PDF exists in s3 bucket.")

        file_textract = self.TEXTRACT_JSON.format(hash=file_hash)

        # check if temp file already processed in s3
        if not exists_file(bucket=AWS_BUCKET_NAME, object_name=file_textract):
            self.dispatch("welp", "textract", "OCR does not exist in s3 bucket.")

            self.dispatch(
                "welp",
                "textract",
                "Textract is detecting text. This might take a few minutes.",
            )

            blocks = process_file(bucket=AWS_BUCKET_NAME, object_name=file_temp)
            self.dispatch("welp", "textract", "OCR text processed.")

            text = json.dumps(blocks)
            upload_text(text=text, bucket=AWS_BUCKET_NAME, object_name=file_textract)
            self.dispatch("welp", "s3", "OCR text saved to s3 bucket.")

        else:
            self.dispatch("welp", "s3", "OCR exists in s3 bucket.")

            text = read_file(bucket=AWS_BUCKET_NAME, object_name=file_textract)
            self.dispatch("welp", "s3", "OCR text loaded from s3 bucket.")

            blocks = json.loads(text)

        self.dispatch("welp", "script", "Processing CV started.")

        # extract information from text
        result = self.process_blocks(blocks)

        # append meta information
        result["meta"] = meta

        # save parsed pdf
        self.dispatch("welp", "script", "Generating Parsed PDF.")
        parsed_path = Path(file_path).parent / (Path(file_path).stem + "-parsed.pdf")
        data2pdf(result, parsed_path)

        # s3 object names
        folder_name = (
            ("{hash} ({name})".format(name=result["name"], hash=file_hash))
            if result["name"]
            else file_hash
        )

        file_original = self.ORIGINAL_FILE.format(name=folder_name)
        file_textract = self.TEXTRACT_FILE.format(name=folder_name)
        file_parsed_json = self.PARSED_JSON.format(name=folder_name)
        file_parsed_pdf = self.PARSED_PDF.format(name=folder_name)

        # upload original file
        upload_file(
            file_path=file_path, bucket=AWS_BUCKET_NAME, object_name=file_original
        )
        self.dispatch("uploaded:cv", "s3", "CV uploaded to s3 bucket.", file_original)

        # upload textract result
        upload_text(
            text=json.dumps(blocks), bucket=AWS_BUCKET_NAME, object_name=file_textract
        )
        self.dispatch(
            "uploaded:textract",
            "s3",
            "Textract result uploaded to s3 bucket.",
            file_textract,
        )

        # upload parsed json result
        upload_text(
            text=json.dumps(result),
            bucket=AWS_BUCKET_NAME,
            object_name=file_parsed_json,
        )
        self.dispatch(
            "uploaded:parsed_json",
            "s3",
            "Processed result uploaded to s3 bucket.",
            file_parsed_json,
        )

        # upload parsed pdf result
        upload_file(
            file_path=parsed_path, bucket=AWS_BUCKET_NAME, object_name=file_parsed_pdf
        )
        self.dispatch(
            "uploaded:parsed_pdf",
            "s3",
            "Processed result uploaded to s3 bucket.",
            file_parsed_pdf,
            {"filename": (Path(file_path).stem + "-parsed.pdf")},
        )

        self.dispatch("script:done", "script", "Processing CV complete.")

        return result


if __name__ == "__main__":
    parser = Parser()
    parser.process_cv(sys.argv[1])
