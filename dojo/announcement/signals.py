from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from dojo.models import Announcement, Dojo_User, UserAnnouncement


@receiver(post_save, sender=Dojo_User)
def add_announcement_to_new_user(sender, instance, **kwargs):
    announcements = Announcement.objects.all()
    if announcements.count() > 0:
        dojo_user = Dojo_User.objects.get(id=instance.id)
        announcement = announcements.first()
        cloud_announcement = (
            "DefectDojo Pro Cloud and On-Premise Subscriptions Now Available!"
            in announcement.message
        )
        if not cloud_announcement or settings.CREATE_CLOUD_BANNER:
            user_announcements = UserAnnouncement.objects.filter(
                user=dojo_user, announcement=announcement,
            )
            if user_announcements.count() == 0:
                UserAnnouncement.objects.get_or_create(
                    user=dojo_user, announcement=announcement,
                )


@receiver(post_save, sender=Announcement)
def announcement_post_save(sender, instance, created, **kwargs):
    if created:
        UserAnnouncement.objects.bulk_create(
            [
                UserAnnouncement(
                    user=user_id, announcement=instance,
                )
                for user_id in Dojo_User.objects.all()
            ],
        )
