# Copyright (C) 2026 by xcentaurix

from Components.config import ConfigSelection


def setupLocationSlots(config_subsection, field_prefix, names, count, none_label, first_default=""):
    """Create `count` ConfigSelection slots (``<field_prefix>1`` .. ``<field_prefix><count>``)
    that let a user pick up to `count` distinct entries from `names` (an ordered
    {code: label} dict) for simultaneous LiveTV bouquets: each slot's choices
    exclude whatever the other slots already picked, and a slot only offers
    "None" once every slot after it is also empty (so filling one slot reveals
    exactly one more empty slot at the end, instead of everywhere at once).

    Returns a `getSelected(skip=0)` function -- the list of chosen values, with
    slot `skip` excluded -- for use as the plugin's own getselectedregions()/
    getselectedcountries().
    """
    choices_list = [("", none_label)] + list(names.items())

    def getSelected(skip=0):
        return [getattr(config_subsection, field_prefix + str(n)).value for n in range(1, count + 1) if n != skip]

    def _autoSlot(_configElement):
        for idx in range(1, count + 1):
            selected = getSelected(idx)
            getattr(config_subsection, field_prefix + str(idx)).setChoices(
                [x for x in choices_list if x[0] and x[0] not in selected or not x[0] and (idx == count or not getattr(config_subsection, field_prefix + str(idx + 1)).value)]
            )

    for n in range(1, count + 1):
        setattr(config_subsection, field_prefix + str(n), ConfigSelection(default=first_default if n == 1 else "", choices=choices_list))

    for n in range(1, count + 1):
        getattr(config_subsection, field_prefix + str(n)).addNotifier(_autoSlot, initial_call=n == count)

    return getSelected
