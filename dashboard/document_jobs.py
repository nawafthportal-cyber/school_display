"""Resumable private-cache jobs, advanced by one bounded browser request."""
from .document_batches import MAX_PARTS, divide_part, extract_part, merge_extractions
from .document_import import DocumentBatchTooLarge, DocumentImportError, MAX_ROWS

JOB_TTL = 60 * 60


def job_key(user_id, school_id, token):
    return f"school-document-job:{user_id}:{school_id}:{token}"


def process_next_part(job):
    if job["next"] >= len(job["parts"]):
        return True
    part = job["parts"][job["next"]]
    try:
        result = extract_part(part, mode=job["mode"])
    except DocumentBatchTooLarge:
        children = divide_part(part)
        if len(job["parts"])+1 > MAX_PARTS:
            raise DocumentImportError("تجاوز الملف الحد الآمن لعدد دفعات القراءة.")
        job["parts"][job["next"]:job["next"]+1] = children
        job["state"] = "running"
        return False
    if sum(len(item["rows"]) for item in job["results"])+len(result["rows"]) > MAX_ROWS*2:
        raise DocumentImportError("يتجاوز الملف سعة المعالجة والمعاينة المتاحة.")
    job["results"].append(result)
    job["next"] += 1
    job["state"] = "running"
    return job["next"] == len(job["parts"])


def finish_extraction(job):
    if job["next"] != len(job["parts"]):
        raise DocumentImportError("لم تكتمل قراءة كل الأجزاء بعد. استأنف القراءة قبل المراجعة.")
    result = merge_extractions(job["results"])
    result["coverage"] = {"completed": job["next"], "total": len(job["parts"])}
    return result
