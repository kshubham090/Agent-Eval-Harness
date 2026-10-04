"""Normalize a title for use in a URL. This starter has intentional defects."""


def slugify(text):
    return text.lower().replace(" ", "-")
