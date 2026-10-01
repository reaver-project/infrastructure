runner_classes = {
    "builder-medium": ("builder", "medium"),
    "builder-large": ("builder", "large"),
    "validation-medium": ("validation", "medium"),
    "validation-large": ("validation", "large"),
}

runner_class_prefix = "reaveros-class-"


def select_runner_class(labels):
    matches = [label for label in labels if label.startswith(runner_class_prefix)]
    if len(matches) > 1:
        raise ValueError("workflow job requests multiple runner classes")
    if not matches:
        return None
    runner_class = matches[0].removeprefix(runner_class_prefix)
    if runner_class not in runner_classes:
        raise ValueError("workflow job requests an unsupported runner class")
    return runner_class


def expected_runner_labels(runner_name, runner_class):
    labels = {"self-hosted", "reaveros-aws", runner_name}
    if runner_class is not None:
        if runner_class not in runner_classes:
            raise ValueError("unsupported runner class")
        labels.add(f"{runner_class_prefix}{runner_class}")
    return labels
